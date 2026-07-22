/**
 * 백엔드 REST 공통 클라이언트 (스펙 v1.3 §2 §4).
 * 401/402 는 여기서 단 한 번 `AuthError` 로 번역되고, 어느 호출부에서도 삼켜지지 않는다
 * (스펙 v1.3 §5 — 401/402 UX, silent fail 금지).
 */
import { AUTH_ERROR_MESSAGE, AuthError, authErrorKindFromStatus } from '@contracts';
import { UserFacingError } from '@main/util/errors';

export interface HttpOptions {
  method?: 'GET' | 'POST';
  /** 토큰이 필요한 요청이면 넘긴다. */
  token?: string;
  body?: RequestInit['body'];
  /**
   * JSON 본문. `content-type: application/json` 을 함께 붙인다.
   * 이걸 두지 않으면 호출부마다 직렬화와 헤더를 따로 챙기게 되고, 헤더를 빠뜨리면
   * 서버가 본문을 못 읽어 422 로 떨어진다.
   */
  json?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** 실패 시 사용자에게 보여줄 한국어 문장 */
  failureMessage: string;
}

/** 상태코드 검사까지 끝난 Response 를 돌려준다. */
export async function httpRequest(url: string, options: HttpOptions): Promise<Response> {
  const headers: Record<string, string> = { ...options.headers };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  let body = options.body;
  if (options.json !== undefined) {
    headers['content-type'] = 'application/json';
    body = JSON.stringify(options.json);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') throw err;
    throw new UserFacingError(
      '서버에 연결하지 못했습니다. 네트워크 상태를 확인한 뒤 다시 시도해 주세요.',
      { cause: err },
    );
  }

  if (!response.ok) {
    const serverMessage = await responseMessage(response);
    const kind = authErrorKindFromStatus(response.status);
    if (kind) throw new AuthError(kind, serverMessage ?? AUTH_ERROR_MESSAGE[kind]);
    throw new UserFacingError(
      serverMessage ?? `${options.failureMessage} (서버 응답 ${response.status})`,
    );
  }
  return response;
}

export async function httpJson<T>(url: string, options: HttpOptions): Promise<T> {
  const response = await httpRequest(url, options);
  try {
    return (await response.json()) as T;
  } catch (err) {
    throw new UserFacingError(`${options.failureMessage} (응답 형식이 올바르지 않습니다)`, {
      cause: err,
    });
  }
}

/** 끝의 `/` 를 정리해 URL 을 잇는다. */
export function joinUrl(baseUrl: string, pathname: string): string {
  return `${baseUrl.replace(/\/+$/, '')}/${pathname.replace(/^\/+/, '')}`;
}

async function responseMessage(response: Response): Promise<string | null> {
  try {
    const value = (await response.clone().json()) as {
      detail?: { message?: unknown } | string;
      message?: unknown;
    };
    if (typeof value.detail === 'object' && typeof value.detail?.message === 'string') {
      return value.detail.message;
    }
    if (typeof value.detail === 'string') return value.detail;
    return typeof value.message === 'string' ? value.message : null;
  } catch {
    return null;
  }
}
