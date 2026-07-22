import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { shell } from 'electron';
import {
  AUTH_ERROR_MESSAGE,
  AuthError,
  type LoginRequest,
  type Session,
  type Subscription,
} from '@contracts';
import { httpJson, httpRequest, joinUrl } from '@main/net/http';
import { UserFacingError, logError } from '@main/util/errors';
import {
  createOAuthLoopbackReceiver,
  OAuthCancelledError,
  type OAuthLoopbackReceiver,
} from './oauth-loopback';
import type { AuthStorage } from './secret-store';

export { MemoryAuthStorage, SafeStorageAuthStorage } from './secret-store';
export type { AuthStorage } from './secret-store';
export { MemoryLlmKeyStore, SafeStorageLlmKeyStore, LLM_PROVIDERS } from './llm-key';
export type { LlmKeyStore } from './llm-key';

/**
 * L4 — Google OAuth 로그인 (cloud-api).
 *
 * **Supabase 를 쓰지 않는다** (백엔드 `docs/authentication.md` 의 결정). 역할 분담:
 *   Electron   — 시스템 브라우저 · loopback 콜백 · PKCE verifier 생성 · 토큰의 OS 보안 저장
 *   cloud-api  — authorization URL 생성 · code 교환 · Google ID token 검증 · 세션 발급
 *
 * Google 의 access/refresh token 은 이 앱에 오지 않는다. 우리가 쥐는 것은 cloud-api 가 발급한
 * **불투명 토큰**뿐이고, refresh 는 쓸 때마다 회전한다.
 */

/** cloud-api 응답 모양 (`cloud-api/src/codegate_cloud_api/models.py`). */
interface SessionResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

interface UserResponse {
  authenticated: boolean;
  subject_id: string;
  email: string;
  name?: string | null;
  picture?: string | null;
}

interface OAuthStartResponse {
  authorization_url: string;
  state: string;
  expires_at: string;
}

/** 저장 키 — 값은 safeStorage 로 암호화되어 `auth.bin` 에 들어간다. */
const KEY_ACCESS = 'codegate.access_token';
const KEY_REFRESH = 'codegate.refresh_token';
const KEY_EXPIRES = 'codegate.access_expires_at';

/** 만료 직전에 미리 갱신한다 — 경계에서 401 을 맞고 사용자가 튕기는 것을 막는다. */
const REFRESH_SKEW_MS = 60_000;

export type AuthMode = 'local' | 'cloud';

export interface AuthServiceOptions {
  authMode?: AuthMode;
  cloudApiUrl: string;
  store: AuthStorage;
  onSessionChanged?: (session: Session) => void;
  openExternal?: (url: string) => Promise<void>;
}

const LOGGED_OUT: Session = { authenticated: false };

const LOCAL_SESSION: Session = {
  authenticated: true,
  subjectId: 'local-user',
  tenantId: 'local',
  readAccess: ['public', 'internal'],
  writeScope: 'workspace',
  provisioned: true,
  authzSource: 'local-workspace',
};

export class AuthService {
  private session: Session = LOGGED_OUT;
  /** 콜백 포트가 하나뿐이라 진행 중인 로그인도 하나뿐이다. */
  private pendingLogin: OAuthLoopbackReceiver | null = null;

  constructor(private readonly options: AuthServiceOptions) {}

  private get local(): boolean {
    return this.options.authMode !== 'cloud';
  }

  async restore(): Promise<Session> {
    if (this.local) return this.setSession(LOCAL_SESSION);

    const refresh = await this.options.store.getItem(KEY_REFRESH);
    if (!refresh) return this.setSession(LOGGED_OUT);
    try {
      // 저장된 access token 은 앱이 꺼져 있는 동안 만료됐을 수 있다. refresh 로 시작한다.
      await this.rotate(refresh);
      return this.setSession(await this.fetchMe());
    } catch (error) {
      logError('auth:restore', error);
      await this.options.store.clear();
      return this.setSession(LOGGED_OUT);
    }
  }

  getSession(): Session {
    return this.session;
  }

  async login(_request: LoginRequest): Promise<Session> {
    if (this.local) return this.setSession(LOCAL_SESSION);

    /*
     * 로그인은 **한 번에 하나만** 진행한다.
     *
     * 콜백 주소가 Google 콘솔에 등록된 고정 포트라 동시에 두 개를 열 수 없다. 사용자가
     * 브라우저를 그냥 닫으면 이전 시도는 콜백을 기다리며 포트를 계속 붙들고 있고, 그
     * 상태에서 다시 누르면 EADDRINUSE 로 "다른 프로그램이 쓰고 있다"는 엉뚱한 오류가 났다.
     * 그래서 새 시도가 이전 시도를 걷어내고 시작한다.
     */
    await this.cancelPendingLogin();
    const receiver = await createOAuthLoopbackReceiver();
    this.pendingLogin = receiver;
    try {
      // PKCE 는 우리가 만든다 — verifier 는 이 프로세스를 떠나지 않고 challenge 만 보낸다.
      const verifier = randomBytes(32).toString('base64url');
      const challenge = createHash('sha256').update(verifier).digest('base64url');

      const start = await httpJson<OAuthStartResponse>(
        joinUrl(this.options.cloudApiUrl, 'auth/google/start'),
        {
          method: 'POST',
          json: { redirect_uri: receiver.redirectTo, code_challenge: challenge },
          failureMessage: 'Google 로그인을 시작하지 못했습니다.',
        },
      );

      this.assertTrustedAuthorizationUrl(start.authorization_url);
      await (this.options.openExternal ?? shell.openExternal)(start.authorization_url);

      const callback = await receiver.waitForCode();
      // state가 **없는 콜백도 거부**한다. 예전 조건은 값이 있을 때만 비교해서, state를
      // 통째로 뺀 콜백이 검사를 우회했다. 비교는 고정 길이 digest끼리 상수시간으로 한다.
      if (!oauthStateMatches(callback.state, start.state)) {
        throw new Error('OAuth state가 없거나 일치하지 않습니다.');
      }

      const session = await httpJson<SessionResponse>(
        joinUrl(this.options.cloudApiUrl, 'auth/google/exchange'),
        {
          method: 'POST',
          json: { state: start.state, code: callback.code, code_verifier: verifier },
          failureMessage: 'Google 로그인을 완료하지 못했습니다.',
        },
      );

      await this.persist(session);
      return this.setSession(await this.fetchMe());
    } catch (error) {
      // 새 로그인이 이 시도를 걷어낸 경우다 — 실패가 아니라 취소이므로 그렇게 말한다.
      if (error instanceof OAuthCancelledError) {
        throw new UserFacingError('로그인이 취소되었습니다.', { cause: error });
      }
      throw new UserFacingError('Google 로그인에 실패했습니다. 다시 시도해 주세요.', {
        cause: error,
      });
    } finally {
      // 나를 걷어낸 새 시도의 receiver 까지 닫아 버리지 않도록 내 것일 때만 비운다.
      if (this.pendingLogin === receiver) this.pendingLogin = null;
      await receiver.close();
    }
  }

  /** 진행 중이던 로그인을 걷어내 고정 콜백 포트를 돌려준다. */
  private async cancelPendingLogin(): Promise<void> {
    const pending = this.pendingLogin;
    if (!pending) return;
    this.pendingLogin = null;
    await pending.close().catch((error: unknown) => logError('auth:cancel-pending', error));
  }

  async logout(): Promise<void> {
    if (this.local) {
      this.setSession(LOCAL_SESSION);
      return;
    }
    const access = await this.options.store.getItem(KEY_ACCESS);
    if (access) {
      // 서버 폐기가 실패해도 로컬 세션은 반드시 지운다 — 화면상 로그아웃은 보장한다.
      try {
        // 204 No Content 라 본문이 없다 — httpJson 은 파싱에서 실패한다.
        await httpRequest(joinUrl(this.options.cloudApiUrl, 'auth/logout'), {
          method: 'POST',
          token: access,
          failureMessage: '로그아웃을 완료하지 못했습니다.',
        });
      } catch (error) {
        logError('auth:logout', error);
      }
    }
    await this.options.store.clear();
    this.setSession(LOGGED_OUT);
  }

  /** 백엔드를 부를 때 쓰는 access token. 만료가 가까우면 먼저 회전시킨다. */
  async getToken(): Promise<string> {
    if (this.local) {
      // 로컬 모드에는 토큰이 없다. 있는 척하면 호출부가 조용히 잘못된 요청을 보낸다.
      throw new AuthError('unauthenticated', AUTH_ERROR_MESSAGE.unauthenticated);
    }
    const access = await this.options.store.getItem(KEY_ACCESS);
    const expiresAt = Number((await this.options.store.getItem(KEY_EXPIRES)) ?? 0);
    if (access && Date.now() < expiresAt - REFRESH_SKEW_MS) return access;

    const refresh = await this.options.store.getItem(KEY_REFRESH);
    if (!refresh) {
      await this.logout();
      throw new AuthError('unauthenticated', AUTH_ERROR_MESSAGE.unauthenticated);
    }
    try {
      return await this.rotate(refresh);
    } catch (error) {
      logError('auth:refresh', error);
      await this.logout();
      throw new AuthError('unauthenticated', AUTH_ERROR_MESSAGE.unauthenticated);
    }
  }

  async ensureActiveSubscription(): Promise<Subscription> {
    await this.getToken();
    if (!this.session.provisioned) {
      throw new AuthError('not_provisioned', AUTH_ERROR_MESSAGE.not_provisioned);
    }
    return { plan: 'Folding', expiresAt: '', active: true };
  }

  async handleAuthError(error: AuthError): Promise<void> {
    if (error.kind === 'unauthenticated') {
      await this.logout();
      return;
    }
    if (error.kind === 'not_provisioned') {
      this.setSession({ ...this.session, provisioned: false, writeScope: 'none' });
    }
  }

  /* ---------------------------------------------------------------- 내부 */

  /** refresh token 회전 — 서버가 새 쌍을 주고 이전 토큰은 폐기한다. */
  private async rotate(refreshToken: string): Promise<string> {
    const next = await httpJson<SessionResponse>(joinUrl(this.options.cloudApiUrl, 'auth/refresh'), {
      method: 'POST',
      json: { refresh_token: refreshToken },
      failureMessage: '세션을 갱신하지 못했습니다.',
    });
    await this.persist(next);
    return next.access_token;
  }

  private async persist(session: SessionResponse): Promise<void> {
    await this.options.store.setItem(KEY_ACCESS, session.access_token);
    await this.options.store.setItem(KEY_REFRESH, session.refresh_token);
    await this.options.store.setItem(KEY_EXPIRES, String(Date.now() + session.expires_in * 1000));
  }

  private async fetchMe(): Promise<Session> {
    const access = await this.options.store.getItem(KEY_ACCESS);
    if (!access) throw new AuthError('unauthenticated', AUTH_ERROR_MESSAGE.unauthenticated);

    const me = await httpJson<UserResponse>(joinUrl(this.options.cloudApiUrl, 'auth/me'), {
      token: access,
      failureMessage: '계정 정보를 확인하지 못했습니다.',
    });

    return {
      authenticated: true,
      email: me.email,
      subjectId: me.subject_id,
      /*
       * cloud-api 는 아직 **권한을 발급하지 않는다** — `UserResponse` 에 tenant·scope 필드가
       * 없다. 그래서 검증된 Google 세션을 로컬 작업 공간 전체 권한으로 취급한다.
       * 서버가 구독·권한을 내려주기 시작하면 아래 세 줄이 그 값으로 바뀐다.
       */
      writeScope: 'workspace',
      provisioned: true,
      authzSource: 'cloud-api/google',
    };
  }

  /** 백엔드가 준 URL 이 정말 Google 로 가는지 열기 전에 확인한다. */
  private assertTrustedAuthorizationUrl(value: string): void {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') {
      throw new Error('신뢰할 수 없는 authorization URL 입니다.');
    }
  }

  private setSession(session: Session): Session {
    this.session = session;
    this.options.onSessionChanged?.(session);
    return session;
  }
}

/**
 * Google 콜백 state 검사.
 *
 * 누락은 즉시 거부하고, 값 비교는 입력 길이와 무관한 SHA-256 digest 두 개를
 * `timingSafeEqual`로 비교한다. 이 함수는 login()이 실제로 쓰며 회귀 테스트에서도 쓴다.
 */
export function oauthStateMatches(callbackState: string | null, expectedState: string): boolean {
  if (!callbackState) return false;
  const left = createHash('sha256').update(callbackState, 'utf8').digest();
  const right = createHash('sha256').update(expectedState, 'utf8').digest();
  return timingSafeEqual(left, right);
}
