import { randomUUID } from 'node:crypto';
import { httpJson, joinUrl } from '@main/net/http';

export interface EvidenceResponse {
  chunk_id: string;
  section_id: string;
  section: string;
  heading_path: string[];
  quote: string;
}

export interface DocumentResponse {
  document_id: string;
  revision: string;
  display_path: string;
  source_uri: string;
  graph_version: string;
  evidence: EvidenceResponse[];
  can_write: boolean;
}

export interface ChangePlanResponse {
  change_plan_id: string;
  document_id: string;
  source_uri: string;
  unified_diff: string;
  plan_hash: string;
}

export interface ChatResponse {
  conversation_id: string;
  message_id: string;
  response_type: 'location_result' | 'target_selection' | 'change_preview' | 'error';
  assistant_text: string;
  documents: DocumentResponse[];
  change_plan: ChangePlanResponse | null;
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface ExecutionResponse {
  execution_id: string;
  document_id: string;
  status: string;
  sync_status: string;
  terminal: boolean;
  stage: string;
  recommended_poll_after_ms: number | null;
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface HealthResponse {
  status: string;
  knowledge_version: string;
  worker_available: boolean;
  agent_available: boolean;
  persistence_available: boolean;
}

export class SidecarClient {
  constructor(private readonly baseUrl: string) {}

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return httpJson(joinUrl(this.baseUrl, 'health'), {
      signal,
      failureMessage: '로컬 Folding 서비스 상태를 확인하지 못했습니다.',
    });
  }

  chat(conversationId: string, message: string, signal?: AbortSignal): Promise<ChatResponse> {
    return httpJson(joinUrl(this.baseUrl, 'chat/messages'), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'idempotency-key': randomUUID(),
      },
      body: JSON.stringify({ conversation_id: conversationId, message }),
      signal,
      failureMessage: '로컬 문서 에이전트가 답변하지 못했습니다.',
    });
  }

  approve(plan: ChangePlanResponse, signal?: AbortSignal): Promise<ExecutionResponse> {
    return httpJson(joinUrl(this.baseUrl, `change-plans/${plan.change_plan_id}/approve`), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'idempotency-key': randomUUID(),
      },
      body: JSON.stringify({ plan_hash: plan.plan_hash }),
      signal,
      failureMessage: '문서 변경을 승인하지 못했습니다.',
    });
  }

  reject(plan: ChangePlanResponse, signal?: AbortSignal): Promise<void> {
    return httpJson(joinUrl(this.baseUrl, `change-plans/${plan.change_plan_id}/reject`), {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'idempotency-key': randomUUID(),
      },
      body: JSON.stringify({ plan_hash: plan.plan_hash, reason: '사용자가 승인 화면에서 거부함' }),
      signal,
      failureMessage: '문서 변경 거부를 기록하지 못했습니다.',
    }).then(() => undefined);
  }

  execution(executionId: string, signal?: AbortSignal): Promise<ExecutionResponse> {
    return httpJson(joinUrl(this.baseUrl, `executions/${executionId}`), {
      signal,
      failureMessage: '문서 변경 상태를 확인하지 못했습니다.',
    });
  }

  retry(executionId: string, signal?: AbortSignal): Promise<ExecutionResponse> {
    return httpJson(joinUrl(this.baseUrl, `executions/${executionId}/retry-sync`), {
      method: 'POST',
      signal,
      failureMessage: '문서 동기화를 다시 시작하지 못했습니다.',
    });
  }

  undo(executionId: string, signal?: AbortSignal): Promise<ExecutionResponse> {
    return httpJson(joinUrl(this.baseUrl, `executions/${executionId}/undo`), {
      method: 'POST',
      headers: { 'idempotency-key': randomUUID() },
      signal,
      failureMessage: '문서 변경을 되돌리지 못했습니다.',
    });
  }

  async waitForExecution(
    initial: ExecutionResponse,
    signal?: AbortSignal,
  ): Promise<ExecutionResponse> {
    let current = initial;
    const deadline = Date.now() + 5 * 60 * 1000;
    while (!current.terminal) {
      if (Date.now() >= deadline) throw new Error('문서 동기화 상태 확인 시간이 초과되었습니다.');
      await delay(
        Math.min(Math.max(current.recommended_poll_after_ms ?? 500, 100), 5_000),
        signal,
      );
      current = await this.execution(current.execution_id, signal);
    }
    return current;
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
