import type {
  AgentEventEnvelope,
  ApprovalEnvelope,
  BuildState,
  ChatMessage,
  Conversation,
  ExecutionSummary,
  FileNode,
  LlmKeyInput,
  LlmKeyStatus,
  LlmProvider,
  LoginRequest,
  Root,
  ScanPreview,
  Session,
  WikiGraph,
} from '@contracts';

export interface CodegateApi {
  auth: {
    login(request: LoginRequest): Promise<Session>;
    logout(): Promise<void>;
    session(): Promise<Session>;
    onChanged(callback: (session: Session) => void): () => void;
  };
  roots: {
    list(): Promise<Root[]>;
    pickDialog(): Promise<string | null>;
    scanPreview(path: string): Promise<ScanPreview>;
    add(path: string): Promise<Root>;
    remove(id: string): Promise<void>;
  };
  build: {
    trigger(): Promise<void>;
    state(): Promise<BuildState>;
    onChanged(callback: (state: BuildState) => void): () => void;
  };
  llmKey: {
    status(): Promise<LlmKeyStatus[]>;
    set(input: LlmKeyInput): Promise<LlmKeyStatus[]>;
    clear(provider: LlmProvider): Promise<LlmKeyStatus[]>;
  };
  tree: {
    get(): Promise<FileNode[]>;
    onChanged(callback: (tree: FileNode[]) => void): () => void;
  };
  chat: {
    list(): Promise<Conversation[]>;
    create(): Promise<Conversation>;
    delete(conversationId: string): Promise<void>;
    messages(conversationId: string): Promise<ChatMessage[]>;
    send(conversationId: string, text: string): Promise<{ messageId: string }>;
    abort(conversationId: string): Promise<void>;
    onAgentEvent(callback: (event: AgentEventEnvelope) => void): () => void;
  };
  execution: {
    retry(messageId: string, executionId: string): Promise<ExecutionSummary>;
    undo(messageId: string, executionId: string): Promise<ExecutionSummary>;
  };
  approval: {
    onRequest(callback: (request: ApprovalEnvelope) => void): () => void;
    respond(id: string, approved: boolean): Promise<void>;
  };
  wiki: {
    /** 활성 빌드의 지식 그래프 (그래프 탭) */
    graph(): Promise<WikiGraph>;
  };
  document: {
    /** 답변을 한글 문서(HWPX)로 저장. 취소하면 null, 저장하면 그 경로. */
    saveHwpx(
      markdown: string,
      templateSourcePath?: string,
      userQuery?: string,
    ): Promise<string | null>;
    /** 한글 양식을 복사해 채운 사본을 만든다. 원본 양식은 건드리지 않는다. */
    fillTemplate(markdown: string): Promise<string | null>;
  };
  shell: {
    openOriginal(relativePath: string): Promise<void>;
    /** 파일 탐색기에서 선택된 채로 보여주기 — 옆의 `.bak` 백업을 찾을 때 쓴다 */
    revealOriginal(relativePath: string): Promise<void>;
  };
}
