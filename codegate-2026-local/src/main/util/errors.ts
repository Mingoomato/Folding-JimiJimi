/**
 * 사용자에게 보이는 오류는 반드시 한국어 문장이어야 한다 (스펙 v1.3 §5 — silent fail 금지).
 * raw stack 이 렌더러로 새어나가지 않도록 여기서 한 번 걸러준다.
 */

/** 이미 한국어 문장으로 다듬어진 오류. */
export class UserFacingError extends Error {
  readonly userFacing = true;
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = 'UserFacingError';
  }
}

function isUserFacing(err: unknown): err is UserFacingError {
  return typeof err === 'object' && err !== null && 'userFacing' in err;
}

/**
 * 임의의 throw 값을 사용자 문장으로 바꾼다.
 * `fallback` 은 원인을 모를 때 보여줄 한국어 문장.
 */
export function toUserMessage(err: unknown, fallback: string): string {
  if (isUserFacing(err)) return err.message;
  if (err instanceof Error && err.name === 'AuthError') return err.message;
  return fallback;
}

/** 디버깅용 상세 로그 — 사용자에게는 절대 보이지 않는다. */
export function logError(scope: string, err: unknown): void {
  const detail = err instanceof Error ? (err.stack ?? err.message) : String(err);
  console.error(`[codegate:${scope}] ${detail}`);
}
