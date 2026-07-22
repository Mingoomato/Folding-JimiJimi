import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';

export interface OAuthCallback {
  code: string;
  /**
   * Google 이 돌려준 `state`. 이 흐름이 **우리가 시작한 그 흐름인가**를 가리는 검사다.
   * (그 뒤를 PKCE `code_verifier` 가 한 번 더 받친다.)
   */
  state: string | null;
}

export interface OAuthLoopbackReceiver {
  redirectTo: string;
  waitForCode(): Promise<OAuthCallback>;
  close(): Promise<void>;
}

/**
 * 기다리던 로그인이 **버려졌을 때** 나는 오류 — 실패가 아니라 취소다.
 *
 * 콜백 주소가 고정이라 포트도 하나뿐이다. 그래서 새 로그인은 이전 시도를 반드시
 * 걷어내야 하고, 걷어낸 쪽은 "실패했다"가 아니라 "취소됐다"고 말해야 한다.
 */
export class OAuthCancelledError extends Error {
  constructor() {
    super('로그인이 취소되었습니다.');
    this.name = 'OAuthCancelledError';
  }
}

/**
 * Google Cloud Console 의 "승인된 리디렉션 URI" 와 **글자 그대로 같아야 하는** 주소.
 *
 * Desktop app 클라이언트는 loopback 의 *포트* 는 눈감아 주지만 **경로는 정확히 비교한다.**
 * 예전에는 포트도 경로도 매번 랜덤이었고(`/oauth/callback/<state>`) Google 이 400
 * `redirect_uri_mismatch` 로 거절했다. 콘솔에 등록된 값과 한 글자도 달라선 안 된다.
 *
 * 경로에 실어 두던 난수는 사라졌다. CSRF 방어는 이제 `state` 대조와 PKCE 가 맡는다 —
 * OAuth 가 원래 그러라고 둔 장치다.
 */
export const DEFAULT_OAUTH_REDIRECT_URI = 'http://127.0.0.1:47821/auth/callback';

function configuredRedirectUri(env: NodeJS.ProcessEnv): string {
  return env.CODEGATE_OAUTH_REDIRECT_URI?.trim() || DEFAULT_OAUTH_REDIRECT_URI;
}

/**
 * 로그인 콜백을 받을 loopback 서버를 연다.
 *
 * 주소는 **고정**이다 — 콘솔에 등록된 값과 같아야 하기 때문이다. 그래서 포트가 이미
 * 쓰이고 있으면 다른 포트로 슬쩍 옮기지 않고 **분명한 오류로 멈춘다.** 조용히 옮겨 봐야
 * Google 이 다시 거절할 뿐이고, 사용자는 원인을 알 수 없는 400 을 또 보게 된다.
 *
 * 포트를 `0` 으로 주면 임시 포트를 잡는다 — 테스트에서 포트 충돌을 피하는 용도다.
 */
export async function createOAuthLoopbackReceiver(
  timeoutMs = 3 * 60 * 1000,
  redirectUri: string = configuredRedirectUri(process.env),
): Promise<OAuthLoopbackReceiver> {
  const target = new URL(redirectUri);
  const wantedPort = Number(target.port);
  const callbackPath = target.pathname;

  let resolveCode!: (callback: OAuthCallback) => void;
  let rejectCode!: (error: Error) => void;
  const codePromise = new Promise<OAuthCallback>((resolve, reject) => {
    resolveCode = resolve;
    rejectCode = reject;
  });
  // 시작 요청이 먼저 실패하면 아무도 waitForCode() 를 부르지 않은 채 close() 로 간다.
  // 그때 거절이 미처리로 남아 프로세스를 시끄럽게 만들지 않도록 미리 받아 둔다.
  codePromise.catch(() => undefined);

  const server = createServer((request, response) => {
    const requestUrl = new URL(request.url ?? '/', 'http://127.0.0.1');
    if (request.method !== 'GET' || requestUrl.pathname !== callbackPath) {
      response.writeHead(404).end('Not found');
      return;
    }
    const oauthError = requestUrl.searchParams.get('error_description');
    const code = requestUrl.searchParams.get('code');
    if (oauthError || !code) {
      response.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' });
      response.end('Folding sign-in did not complete. You can close this tab.');
      rejectCode(new Error(oauthError || 'OAuth callback did not include an authorization code'));
      return;
    }
    response.writeHead(200, {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    });
    response.end(
      '<!doctype html><meta charset="utf-8"><title>Folding</title><p>로그인이 완료되었습니다. 이 창을 닫고 Folding으로 돌아가세요.</p>',
    );
    resolveCode({ code, state: requestUrl.searchParams.get('state') });
  });

  await listen(server, wantedPort, redirectUri);
  server.unref();
  const port = wantedPort === 0 ? (server.address() as AddressInfo).port : wantedPort;
  const timer = setTimeout(() => rejectCode(new Error('OAuth login timed out')), timeoutMs);
  timer.unref();

  return {
    redirectTo: `http://127.0.0.1:${port}${callbackPath}`,
    waitForCode: () => codePromise,
    close: async () => {
      clearTimeout(timer);
      // 아직 콜백을 기다리는 중이라면 그 약속부터 끊는다. 안 그러면 기다리던 쪽이
      // 영원히 깨어나지 못하고, 포트를 붙든 채로 남는다.
      rejectCode(new OAuthCancelledError());
      await close(server);
    },
  };
}

function listen(server: Server, port: number, redirectUri: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const onError = (error: NodeJS.ErrnoException) => {
      // 이 포트가 아니면 Google 이 거절한다. 원인을 사용자 문장으로 분명히 말한다.
      reject(
        error.code === 'EADDRINUSE'
          ? new Error(
              `로그인 콜백 포트 ${port} 를 다른 프로그램이 쓰고 있습니다. ` +
                `그 프로그램을 끄고 다시 시도해 주세요. (Google에 등록된 주소: ${redirectUri})`,
            )
          : error,
      );
    };
    server.once('error', onError);
    server.listen(port, '127.0.0.1', () => {
      server.off('error', onError);
      resolve();
    });
  });
}

function close(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}
