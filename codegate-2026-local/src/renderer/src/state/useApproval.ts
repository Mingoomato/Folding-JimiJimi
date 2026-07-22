import { useCallback, useEffect, useState } from 'react';
import type { ApprovalEnvelope } from '@contracts';

/**
 * A3 승인 게이트의 렌더러 쪽 절반.
 *
 * 메인은 응답이 올 때까지 도구 실행을 멈추고 기다린다.
 * 그러므로 여기서 응답을 빠뜨리면 에이전트가 영영 멈춘다 — 모든 종료 경로에서 응답한다.
 */
export function useApproval() {
  const [pending, setPending] = useState<ApprovalEnvelope | null>(null);

  useEffect(() => {
    return window.codegate.approval.onRequest((env) => setPending(env));
  }, []);

  const respond = useCallback(
    async (approved: boolean) => {
      if (!pending) return;
      const { id } = pending;
      setPending(null);
      await window.codegate.approval.respond(id, approved);
    },
    [pending],
  );

  return { pending, approve: () => respond(true), reject: () => respond(false) };
}
