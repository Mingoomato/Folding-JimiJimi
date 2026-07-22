/**
 * 승인 게이트 왕복 (스펙 v1.3 §5 "승인 우회 금지").
 * 쓰기 도구는 반드시 이 브로커를 통해야 하며, 렌더러의 응답 없이는 `true` 가 나오지 않는다.
 * 응답이 오지 않으면 타임아웃으로 `false`(거부)를 돌려준다 — 열린 채 매달리지 않는다.
 */
import { randomUUID } from 'node:crypto';
import type { ApprovalEnvelope, ApprovalRequest } from '@contracts';

export interface ApprovalBrokerOptions {
  /** 렌더러로 `IPC_EVENTS.approvalRequest` 를 보내는 함수 */
  send: (envelope: ApprovalEnvelope) => void;
  /** 응답 대기 제한(ms). 기본 5분. */
  timeoutMs?: number;
}

interface Pending {
  resolve: (approved: boolean) => void;
  timer: NodeJS.Timeout;
}

export class ApprovalBroker {
  private readonly pending = new Map<string, Pending>();

  constructor(private readonly options: ApprovalBrokerOptions) {}

  /** `AgentDeps.approvalHandler` 로 그대로 주입된다. */
  request = (request: ApprovalRequest): Promise<boolean> => {
    const id = randomUUID();
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve(false); // 무응답 = 거부
      }, this.options.timeoutMs ?? 5 * 60 * 1000);
      this.pending.set(id, { resolve, timer });
      this.options.send({ id, request });
    });
  };

  /** 렌더러의 `IPC.approvalRespond` 응답. 모르는 id 는 무시한다. */
  respond(id: string, approved: boolean): void {
    const entry = this.pending.get(id);
    if (!entry) return;
    this.pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve(approved);
  }

  /** 창이 닫히거나 대화가 중단되면 남은 요청을 모두 거부로 정리한다. */
  rejectAll(): void {
    for (const [id, entry] of this.pending) {
      this.pending.delete(id);
      clearTimeout(entry.timer);
      entry.resolve(false);
    }
  }
}
