/**
 * codegate — 레포 간 인터페이스 계약 (스펙 v1.3 §2)
 *
 * 이 파일은 `codegate-2026-local`(우창)과 `codegate-2026-agent`(성주) 두 레포에
 * 동일하게 커밋된다. M0의 완료 기준(DoD)이 바로 이 파일이다.
 *
 * 경계는 셋이다:
 *   1. @codegate/agent 공개 인터페이스   — local(메인) → agent
 *   2. IPC 채널                          — local(메인) ↔ local(렌더러)
 *   3. 백엔드 REST                       — local(메인) → backend
 */

/* ============================================================================
 * 1. @codegate/agent 공개 인터페이스
 * ========================================================================== */

/** 문서 인용 — 답변 말미에 `【DOC-ID rev.N §섹션】` 형태로 표기된다. */
export interface Citation {
  /** 위키 문서 ID (예: `PMC10300442`) */
  docId: string;
  /** 빌드 리비전 — 같은 문서라도 재빌드되면 증가한다 */
  rev: string | number;
  /** 섹션 라벨 (예: `Results 2`) */
  section: string;
  /** Backend snapshot and evidence identifiers. */
  graphVersion?: string;
  chunkId?: string;
  sectionId?: string;
  /**
   * 원본 파일의 등록 폴더 기준 상대 경로.
   * 렌더러에서 인용을 클릭하면 이 경로로 원본을 연다 (스펙 v1.3 §1 L1).
   */
  sourcePath?: string;
}

/** kordoc patch 결과. exit 2 = 일부 편집이 적용되지 않은 부분 실패. */
export interface PatchResult {
  /** exit 0 이면 true. exit 2(부분 실패)면 false. */
  ok: boolean;
  /** kordoc 프로세스 종료 코드. 2 = 미적용 편집 있음 (스펙 v1.3 §5). */
  exitCode: number;
  /** 적용된 편집 수 */
  applied: number;
  /**
   * 적용되지 않은 편집들. exitCode 2 일 때 채워지며,
   * 사용자에게 "부분 실패"로 반드시 표시해야 한다.
   */
  unapplied: string[];
  /** 패치 직전 생성된 `.bak` 백업 경로 */
  backupPath: string;
}

/** L3(kordoc 래퍼) 시그니처 — 메인 프로세스가 구현해 agent에 주입한다. */
export interface KordocApi {
  /** 문서를 마크다운으로 파싱 */
  parse(path: string): Promise<string>;
  /**
   * 편집된 마크다운을 원본 문서에 역적용.
   * 규칙: `.bak` 백업 → 임시파일 작성 → rename(원자적).
   */
  patch(path: string, editedMd: string): Promise<PatchResult>;
  /** 마크다운에서 새 문서 생성 */
  generate(md: string, outPath: string, preset?: string): Promise<void>;
  /** 페이지 이미지 렌더 — **hwpx만 가능**. `.hwp`는 지원하지 않는다. */
  render(path: string, pages?: number[]): Promise<Buffer[]>;
}

/** 쓰기 도구가 승인을 요청할 때 전달되는 페이로드. */
export interface ApprovalRequest {
  /** 요청한 도구 이름 (`patch_document` · `create_document` · `create_from_template`) */
  tool: string;
  /** 대상 파일 절대경로 */
  target: string;
  /** 통합 diff 문자열 — 승인 모달에 그대로 렌더된다 */
  diff: string;
  /** 에이전트가 밝힌 근거 (모달의 "근거" 영역) */
  rationale?: string;
  /** Backend approval integrity fields. */
  changePlanId?: string;
  planHash?: string;
  /** Native document v2 preview metadata. The renderer never receives local artifact paths. */
  documentPreview?: DocumentApprovalPreview;
}

export interface DocumentApprovalDiff {
  operationIndex: number;
  operationType: string;
  locator: Record<string, unknown> | null;
  before: unknown;
  after: unknown;
}

export interface DocumentApprovalImagePair {
  label: string;
  beforeDataUrl?: string;
  afterDataUrl?: string;
  summaryOnly: boolean;
}

export interface DocumentApprovalPreview {
  format: 'hwp' | 'hwpx' | 'docx' | 'pptx' | 'xlsx' | 'pdf';
  capabilityId: string;
  writerFingerprint?: string;
  rendererFingerprint?: string;
  sourceSha256?: string;
  proposedSha256?: string;
  targetRelativePath: string;
  warnings: string[];
  structuralDiff: DocumentApprovalDiff[];
  images: DocumentApprovalImagePair[];
  truncatedCount: number;
}

/** local이 agent에 주입하는 의존성 묶음. */
export interface AgentDeps {
  config: {
    /** 사용자가 등록한 폴더들. 이 밖의 경로 접근은 A3가 거부한다. */
    roots: string[];
    backendUrl: string;
    /** L2가 관리하는 로컬 위키 경로 (`~/.codegate/wiki`) */
    wikiDir: string;
  };
  /** L4 — keychain(safeStorage) 기반, 갱신 포함 */
  auth: { getToken(): Promise<string>; canWrite(): boolean };
  /** L3 */
  kordoc: KordocApi;
  /** L1 — 승인 모달. false 반환 시 도구 실행이 취소된다. */
  approvalHandler: (req: ApprovalRequest) => Promise<boolean>;
}

/**
 * 에이전트가 스트리밍하는 이벤트.
 * 메인 프로세스가 그대로 IPC(`agent:event`)로 렌더러에 중계한다.
 */
export interface AgentSendOptions {
  conversationId: string;
  signal?: AbortSignal;
}

export type AgentEvent =
  | { type: 'text_delta'; text: string }
  | { type: 'tool_start'; name: string; summary: string }
  | { type: 'approval_request'; diff: string; target: string }
  | { type: 'auth_error'; kind: AuthErrorKind }
  | { type: 'done'; citations: Citation[]; execution?: ExecutionSummary };

export interface Agent {
  send(userText: string, options: AgentSendOptions): AsyncIterable<AgentEvent>;
}

export type CreateAgent = (deps: AgentDeps) => Agent;

/* ============================================================================
 * 2. 앱 도메인 모델 (메인 ↔ 렌더러 공유)
 * ========================================================================== */

/** 등록된 감시 폴더. */
export interface Root {
  id: string;
  path: string;
  /** 스캔 시 포함된 파일 수 */
  includedCount: number;
  /** 제외된 파일 수 (지원하지 않는 확장자 등) */
  excludedCount: number;
  addedAt: string;
}

export const SINGLE_ROOT_LIMIT_MESSAGE =
  '현재는 문서 폴더를 하나씩만 사용할 수 있습니다. 기존 폴더를 해제한 뒤 새 폴더를 등록해 주세요.';

/** 스캔 미리보기 — 폴더 등록 시 사용자에게 포함/제외를 확인받는다. */
export interface ScanPreview {
  root: string;
  included: string[];
  excluded: { path: string; reason: string }[];
}

/** 우측 트리에 표시되는 파일별 상태 (스펙 v1.3 §1 L1). */
export type FileStatus = 'pending' | 'converting' | 'done' | 'error';

export interface FileNode {
  /** 등록 폴더 기준 상대 경로 */
  path: string;
  name: string;
  isDir: boolean;
  status: FileStatus;
  children?: FileNode[];
}

/**
 * 로컬 빌드 파이프라인의 3단계 (스펙 v1.4 §0).
 *   convert   — 변환 모듈(민규)이 원본을 INPUT_CONTRACT 마크다운으로
 *   enrich    — enrichment: LLM API 직접 호출 (동시성 제한·재시도)
 *   assemble  — 빌더 코어(용휘)가 불변 빌드로 조립
 */
export type BuildPhase = 'convert' | 'enrich' | 'assemble';

export const BUILD_PHASE_LABEL: Record<BuildPhase, string> = {
  convert: '변환',
  enrich: '분석',
  assemble: '조립',
};

/** v1.4: 업로드·폴링·회수 단계는 사라졌다. 전 과정이 이 컴퓨터에서 돈다. */
export type BuildStatus =
  | 'idle'
  | 'scanning'
  | 'converting'
  | 'enriching'
  | 'assembling'
  | 'done'
  | 'failed';

export interface BuildState {
  buildId: string | null;
  status: BuildStatus;
  /** 전체 진행률 0–100 */
  progress: number;
  /** 현재 단계의 세부 진행 — 렌더러가 "분석 12/37" 처럼 보여준다 */
  phase?: { name: BuildPhase; done: number; total: number };
  message: string;
  /** 실패 시 사용자에게 보여줄 문장 (silent fail 금지 — 스펙 v1.4 §5) */
  error?: string;
  /**
   * enrichment 부분 실패로 보류된 문서 (스펙 v1.4 §5).
   * 한 문서가 실패해도 빌드 전체를 실패시키지 않고 그 문서만 보류한다.
   */
  deferred?: { path: string; reason: string }[];
  updatedAt: string;
}

export interface Subscription {
  /** 예: `Pro`, `Team` */
  plan: string;
  /** ISO date. 만료 표시에 사용 */
  expiresAt: string;
  active: boolean;
}

export interface Session {
  authenticated: boolean;
  email?: string;
  subjectId?: string;
  tenantId?: string;
  readAccess?: string[];
  writeScope?: 'none' | 'documents' | 'workspace';
  writableDocumentIds?: string[];
  provisioned?: boolean;
  authzSource?: string;
}

/** 좌측 채팅 목록의 한 줄. */
export interface Conversation {
  id: string;
  title: string;
  updatedAt: string;
}

export type ChatRole = 'user' | 'assistant';

export interface ChatMessage {
  id: string;
  conversationId: string;
  role: ChatRole;
  text: string;
  /** assistant 메시지에만 채워진다 */
  citations?: Citation[];
  /** 스트리밍 중 표시된 도구 배지 */
  tools?: { name: string; summary: string }[];
  createdAt: string;
  /** 스트리밍이 아직 진행 중인지 */
  streaming?: boolean;
  execution?: ExecutionSummary;
  /**
   * 이 답변이 승인을 거쳐 손댄 문서의 경로 (`ApprovalRequest.target` 과 같은 값).
   * 답변에서 그 문서를 바로 열거나 폴더에서 찾아보게 하기 위한 것이다.
   * 승인 요청 자체가 없었으면 비어 있다.
   */
  changedPath?: string;
}

/* ---- 지식 그래프 (활성 위키 빌드의 manifest + links) ---- */

export interface WikiGraphNode {
  /** 문서 식별자 — 엣지가 이 값으로 노드를 가리킨다 */
  docId: string;
  title: string;
  /** regulation · policy · contract · report … 색 구분에 쓴다 */
  docType: string;
  /** active · draft · archived · superseded */
  status: string;
  /**
   * 원본 파일을 가리키는 URI. **트리에서 고른 파일과 그래프 노드를 잇는 유일한 열쇠다.**
   * 변환 단계에서 원본을 확인하지 못하면 비어 있을 수 있다.
   */
  sourceUri?: string;
  sourceFilename?: string;
  summary?: string;
}

export interface WikiGraphEdge {
  from: string;
  to: string;
  /** references · implements · implemented_by · based_on · supersedes · superseded_by */
  relationType: string;
}

export interface WikiGraph {
  /** 활성 build id. 빌드가 없으면 `null` 이고 노드·엣지도 비어 있다. */
  buildId: string | null;
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export interface ExecutionSummary {
  executionId: string;
  documentId: string;
  status: string;
  stage: string;
  terminal: boolean;
  error?: { code: string; message: string; retryable: boolean };
  canRetry: boolean;
  canUndo: boolean;
}

/* ============================================================================
 * 3. IPC 채널 계약 (메인 ↔ 렌더러)
 * ========================================================================== */

/** 렌더러 → 메인 (invoke/handle, 요청-응답) */
export const IPC = {
  // L4 — 인증
  authLogin: 'auth:login',
  authLogout: 'auth:logout',
  authSession: 'auth:session',

  // L4/L2 — 폴더
  rootsList: 'roots:list',
  rootsPickDialog: 'roots:pickDialog',
  rootsScanPreview: 'roots:scanPreview',
  rootsAdd: 'roots:add',
  rootsRemove: 'roots:remove',

  // L2 — 답변을 한글 문서(HWPX)로 저장
  documentSaveHwpx: 'document:saveHwpx',
  // L2 — 한글 양식을 복사해 채운 사본 만들기 (원본 양식은 건드리지 않는다)
  documentFillTemplate: 'document:fillTemplate',

  // L2 — 로컬 빌드
  buildTrigger: 'build:trigger',
  buildState: 'build:state',

  // L4 — enrichment LLM 키 (자격증명 ②, 스펙 v1.4 §5)
  llmKeyStatus: 'llm:status',
  llmKeySet: 'llm:set',
  llmKeyClear: 'llm:clear',

  // L1 — 트리
  treeGet: 'tree:get',

  // L1 — 채팅
  chatList: 'chat:list',
  chatCreate: 'chat:create',
  chatDelete: 'chat:delete',
  chatMessages: 'chat:messages',
  chatSend: 'chat:send',
  chatAbort: 'chat:abort',

  executionRetry: 'execution:retry',
  executionUndo: 'execution:undo',

  // L2 — sidecar 프로세스 수명주기 (스펙 v2.1 §1 L2)
  sidecarState: 'sidecar:state',
  sidecarRestart: 'sidecar:restart',

  // L1 — 승인 모달 응답
  approvalRespond: 'approval:respond',

  // L1 — 인용 클릭 시 원본 열기
  openOriginal: 'shell:openOriginal',
  /** L1 — 원본을 파일 탐색기에서 선택된 채로 보여준다 (백업 `.bak` 을 찾을 수 있게) */
  revealOriginal: 'shell:revealOriginal',

  // L1 — 활성 빌드의 지식 그래프 (그래프 탭)
  wikiGraph: 'wiki:graph',
} as const;

/** 메인 → 렌더러 (webContents.send, 단방향 푸시) */
export const IPC_EVENTS = {
  /** AgentEvent 를 그대로 중계 (스펙 v1.3 §2) */
  agentEvent: 'agent:event',
  /** 승인이 필요할 때. 렌더러는 IPC.approvalRespond 로 답한다. */
  approvalRequest: 'approval:request',
  buildState: 'build:state:changed',
  treeChanged: 'tree:changed',
  sessionChanged: 'auth:session:changed',
  /** sidecar 기동·재기동 상태 — 엔진이 준비되기 전 UI 가 멈춘 듯 보이지 않게 한다 */
  sidecarState: 'sidecar:state:changed',
} as const;

/* ---------------------------------------------------------------------------
 * sidecar 프로세스 수명주기 (스펙 v2.1 §1 L2)
 *
 * 앱이 `codegate-local` 을 직접 띄우고 지켜본다. 렌더러는 상태만 보며
 * 프로세스를 직접 다루지 않는다.
 * ------------------------------------------------------------------------- */

export type SidecarStatus =
  | 'external'
  | 'stopped'
  | 'starting'
  | 'ready'
  | 'unhealthy'
  | 'crashed'
  | 'restarting';

export interface SidecarLifecycleState {
  status: SidecarStatus;
  /** `http://127.0.0.1:<port>` — 포트는 기동 전에 미리 잡으므로 값이 먼저 정해진다 */
  baseUrl: string | null;
  /** 사용자에게 그대로 보여줄 문장 */
  message: string;
  error?: string;
  /** 비정상 종료 후 되살린 횟수 */
  restarts: number;
  updatedAt: string;
}

export const SIDECAR_STATUS_MESSAGE: Record<SidecarStatus, string> = {
  external: '외부 엔진에 연결했습니다.',
  stopped: '엔진이 꺼져 있습니다.',
  starting: '엔진을 준비하는 중…',
  ready: '준비됨',
  unhealthy: '엔진이 응답하지 않습니다.',
  crashed: '엔진이 예기치 않게 종료됐습니다.',
  restarting: '엔진을 다시 시작하는 중…',
};

/** IPC로 오가는 AgentEvent 봉투 — 어느 대화의 이벤트인지 식별. */
export interface AgentEventEnvelope {
  conversationId: string;
  messageId: string;
  event: AgentEvent;
}

/** 승인 요청 봉투 — `id`로 응답을 짝짓는다. */
export interface ApprovalEnvelope {
  id: string;
  request: ApprovalRequest;
}

/* ============================================================================
 * 4. 서버 REST 스키마 (스펙 v1.4 §2 — 우창 ↔ 용휘)
 *
 * v1.4 에서 서버의 역할은 **로그인·구독 확인뿐**이다. 아래 둘이 서버 API의 전부다.
 * ========================================================================== */

export type OAuthProvider = 'google';

export interface LoginRequest {
  provider: OAuthProvider;
}

/** `GET /me/subscription` */
export interface SubscriptionResponse {
  plan: string;
  expiresAt: string;
  active: boolean;
}

/*
 * 서버 API는 위 둘이 전부다 (스펙 v1.4 §2).
 *
 * v1.3 에 있던 `POST /builds` · `GET /builds/{id}` · `/artifact` · `/ack` 는 삭제됐다.
 * 문서·마크다운·위키는 자사 서버로 전송되지 않는다 — 변환·enrichment·조립이 전부
 * 이 컴퓨터에서 돌기 때문이다. 밖으로 나가는 텍스트는 LLM 제공자로 가는 것뿐이다.
 */

/* ============================================================================
 * 5. enrichment LLM 키 (자격증명 ② — 스펙 v1.4 §5)
 *
 * 자격증명은 셋이고 절대 섞지 않는다:
 *   ① 백엔드 토큰(로그인)        — safeStorage
 *   ② enrichment LLM 키(Gemini 등) — 설정 화면에서 입력, safeStorage   ← 여기
 *   ③ Anthropic 자격증명(SDK 추론) — 환경변수
 * ========================================================================== */

export type LlmProvider = 'gemini' | 'openai' | 'anthropic';

export const LLM_PROVIDER_LABEL: Record<LlmProvider, string> = {
  gemini: 'Google Gemini',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
};

/**
 * 렌더러에 노출되는 키 상태.
 * **원문 키는 절대 렌더러로 내려보내지 않는다** — 설정됨 여부와 끝 4자리만 준다.
 */
export interface LlmKeyStatus {
  provider: LlmProvider;
  configured: boolean;
  /** 끝 4자리 (예: `…8f2a`). 설정돼 있을 때만. */
  hint?: string;
  /**
   * 앱이 구독의 일부로 직접 공급하는 키.
   * 사용자가 입력하지도, 지우지도 못한다 — 화면은 입력폼 대신 안내만 보여준다.
   */
  managed?: boolean;
}

/** 키 저장 요청. 메인이 safeStorage 로 암호화해 보관한다. */
export interface LlmKeyInput {
  provider: LlmProvider;
  apiKey: string;
}

/**
 * 문서 분석용 LLM 키를 구하지 못해 빌드를 시작할 수 없을 때 쓰는 문장.
 *
 * 키는 구독에 포함돼 앱이 내부적으로 공급한다(스펙 v2.4). 사용자가 할 수 있는 일이
 * 없으므로 "키를 입력하라"고 하지 않는다.
 */
export const LLM_KEY_MISSING_MESSAGE =
  '문서 분석 서비스에 연결하지 못했습니다. 구독 상태를 확인하거나 잠시 후 다시 시도해 주세요.';

/** 인증/구독 오류 — 로그인·구독 확인 시 발생한다 (스펙 v1.4 §1 각주). */
export class AuthError extends Error {
  constructor(
    public kind: AuthErrorKind,
    message: string,
  ) {
    super(message);
    this.name = 'AuthError';
  }
}

/** HTTP 상태코드 → AgentEvent 의 auth_error kind 매핑. */
export function authErrorKindFromStatus(
  status: number,
): AuthErrorKind | null {
  if (status === 401) return 'unauthenticated';
  if (status === 402) return 'subscription_expired';
  if (status === 403) return 'not_provisioned';
  return null;
}

/** 401/402 를 사용자 문장으로 — silent fail 금지 (스펙 v1.3 §5). */
export type AuthErrorKind = 'unauthenticated' | 'subscription_expired' | 'not_provisioned';

export const AUTH_ERROR_MESSAGE: Record<AuthErrorKind, string> = {
  unauthenticated: '로그인이 만료되었습니다. 다시 로그인해 주세요.',
  subscription_expired: '구독이 만료되었습니다. 구독을 갱신하면 빌드를 이어갈 수 있어요.',
  not_provisioned: '계정에 Folding 작업 권한이 아직 연결되지 않았습니다.',
};

/** 위키에서 근거를 찾지 못했을 때 에이전트가 쓰는 고정 문구 (AGENT_GUIDE 규칙). */
export const NOT_FOUND_IN_WIKI = '위키에서 확인되지 않음';

/** 인용 렌더 형식 — `【DOC-ID rev.N §섹션】` */
export function formatCitation(c: Citation): string {
  return `【${c.docId} rev.${c.rev} §${c.sectionId ?? c.section}】`;
}
