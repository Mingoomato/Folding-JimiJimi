import { randomUUID } from 'node:crypto';
import { httpJson, httpRequest, joinUrl } from '@main/net/http';

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
  document_plan: DocumentPlanResponse | null;
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

export type NativeDocumentFormat = 'hwp' | 'hwpx' | 'docx' | 'pptx' | 'xlsx' | 'pdf';

export interface DocumentCapabilityResponse {
  capability_id: string;
  format: NativeDocumentFormat;
  operations: string[];
  read: boolean;
  create: boolean;
  mutate: boolean;
  render: boolean;
  active: boolean;
  disabled_reasons: string[];
  writer_name: string | null;
  writer_version: string | null;
  renderer_name: string | null;
  renderer_version: string | null;
  max_source_bytes: number;
  max_result_bytes: number;
  signed_policy: string;
  macro_policy: string;
}

export interface DocumentCapabilitiesResponse {
  snapshot_id: string;
  generated_at: string;
  capabilities: DocumentCapabilityResponse[];
}

export interface DocumentStructuralDiff {
  operation_index: number;
  operation_type: string;
  locator: Record<string, unknown> | null;
  before: unknown;
  after: unknown;
}

export interface DocumentPreviewPair {
  locator_label: string;
  before_artifact_id: string | null;
  after_artifact_id: string | null;
  summary_only: boolean;
}

export interface DocumentPlanResponse {
  change_plan_id: string;
  kind: 'mutation' | 'creation' | 'derivation';
  document_id: string;
  format: NativeDocumentFormat;
  capability_id: string;
  source_uri: string | null;
  target_relative_path: string | null;
  status: 'preparing' | 'pending_approval' | 'approved' | 'rejected' | 'expired' | 'consumed' | 'failed';
  base_sha256: string | null;
  proposed_sha256: string | null;
  plan_hash: string | null;
  graph_version: string;
  capability_snapshot_id: string;
  writer_fingerprint: string | null;
  renderer_fingerprint: string | null;
  operations: Record<string, unknown>[];
  structural_diff: DocumentStructuralDiff[];
  preview_manifest: {
    manifest_sha256: string;
    pairs: DocumentPreviewPair[];
    truncated_count: number;
  } | null;
  warnings: string[];
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface DocumentExecutionResponse {
  execution_id: string;
  change_plan_id: string | null;
  undo_of_execution_id: string | null;
  document_id: string;
  change_kind: 'create' | 'update' | 'derive' | 'recovery_remove';
  format: NativeDocumentFormat;
  capability_id: string;
  source_uri: string;
  status: 'prepared' | 'file_applied' | 'syncing' | 'completed' | 'sync_failed' | 'conflict' | 'failed' | 'undone';
  before_sha256: string | null;
  after_sha256: string | null;
  artifact_sha256: string;
  graph_version_before: string;
  graph_version_after: string | null;
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface DocumentCreationRequest {
  document_id: string;
  format: NativeDocumentFormat;
  capability_id: string;
  capability_snapshot_id: string;
  graph_version: string;
  target_relative_path: string;
  payload:
    | { type: 'document.create_from_markdown/v1'; markdown: string; template_id?: string }
    | { type: 'workbook.create/v1'; sheets: unknown[] }
    | {
        type: 'hwp.derive_hwpx/v1';
        source_document_id: string;
        expected_source_sha256: string;
      };
}

export class SidecarClient {
  private readonly v2BaseUrl: string;

  constructor(private readonly baseUrl: string) {
    this.v2BaseUrl = baseUrl.replace(/\/api\/v1\/?$/, '/api/v2');
  }

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return httpJson(joinUrl(this.baseUrl, 'health'), {
      signal,
      failureMessage: '로컬 Folding 서비스 상태를 확인하지 못했습니다.',
    });
  }

  chat(conversationId: string, message: string, signal?: AbortSignal): Promise<ChatResponse> {
    return httpJson(joinUrl(this.v2BaseUrl, 'chat/messages'), {
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

  documentCapabilities(signal?: AbortSignal): Promise<DocumentCapabilitiesResponse> {
    return httpJson(joinUrl(this.v2BaseUrl, 'document-capabilities'), {
      signal,
      failureMessage: '문서 편집 기능 상태를 확인하지 못했습니다.',
    });
  }

  createDocumentPlan(
    request: DocumentCreationRequest,
    signal?: AbortSignal,
  ): Promise<DocumentPlanResponse> {
    return httpJson(joinUrl(this.v2BaseUrl, 'document-creation-plans'), {
      method: 'POST',
      headers: { 'idempotency-key': randomUUID() },
      json: request,
      signal,
      failureMessage: '문서 생성 계획을 준비하지 못했습니다.',
    });
  }

  documentPlan(planId: string, signal?: AbortSignal): Promise<DocumentPlanResponse> {
    return httpJson(joinUrl(this.v2BaseUrl, `change-plans/${planId}`), {
      signal,
      failureMessage: '문서 변경 계획 상태를 확인하지 못했습니다.',
    });
  }

  approveDocumentPlan(
    plan: DocumentPlanResponse,
    signal?: AbortSignal,
  ): Promise<DocumentExecutionResponse> {
    if (!plan.plan_hash) throw new Error('승인할 plan_hash가 없습니다.');
    return httpJson(joinUrl(this.v2BaseUrl, `change-plans/${plan.change_plan_id}/approve`), {
      method: 'POST',
      headers: { 'idempotency-key': randomUUID() },
      json: { plan_hash: plan.plan_hash },
      signal,
      failureMessage: '승인된 문서를 적용하지 못했습니다.',
    });
  }

  rejectDocumentPlan(plan: DocumentPlanResponse, signal?: AbortSignal): Promise<void> {
    if (!plan.plan_hash) throw new Error('거절할 plan_hash가 없습니다.');
    return httpJson(joinUrl(this.v2BaseUrl, `change-plans/${plan.change_plan_id}/reject`), {
      method: 'POST',
      headers: { 'idempotency-key': randomUUID() },
      json: { plan_hash: plan.plan_hash, reason: 'user_rejected' },
      signal,
      failureMessage: '문서 변경 거절을 기록하지 못했습니다.',
    }).then(() => undefined);
  }

  documentExecution(
    executionId: string,
    signal?: AbortSignal,
  ): Promise<DocumentExecutionResponse> {
    return httpJson(joinUrl(this.v2BaseUrl, `executions/${executionId}`), {
      signal,
      failureMessage: '문서 실행 상태를 확인하지 못했습니다.',
    });
  }

  async previewDataUrl(
    planId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<string> {
    const response = await httpRequest(
      joinUrl(this.v2BaseUrl, `change-plans/${planId}/previews/${artifactId}`),
      { signal, failureMessage: '문서 미리보기를 불러오지 못했습니다.' },
    );
    const mimeType = response.headers.get('content-type')?.split(';')[0] || 'image/png';
    const bytes = Buffer.from(await response.arrayBuffer());
    return `data:${mimeType};base64,${bytes.toString('base64')}`;
  }

  async waitForDocumentPlan(
    initial: DocumentPlanResponse,
    signal?: AbortSignal,
  ): Promise<DocumentPlanResponse> {
    let current = initial;
    const deadline = Date.now() + 90_000;
    while (current.status === 'preparing') {
      if (Date.now() >= deadline) throw new Error('문서 미리보기 준비 시간이 초과되었습니다.');
      await delay(200, signal);
      current = await this.documentPlan(current.change_plan_id, signal);
    }
    return current;
  }

  async waitForDocumentExecution(
    initial: DocumentExecutionResponse,
    signal?: AbortSignal,
  ): Promise<DocumentExecutionResponse> {
    let current = initial;
    const deadline = Date.now() + 5 * 60 * 1000;
    const terminal = new Set(['completed', 'sync_failed', 'conflict', 'failed', 'undone']);
    while (!terminal.has(current.status)) {
      if (Date.now() >= deadline) throw new Error('문서 동기화 상태 확인 시간이 초과되었습니다.');
      await delay(500, signal);
      current = await this.documentExecution(current.execution_id, signal);
    }
    return current;
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
