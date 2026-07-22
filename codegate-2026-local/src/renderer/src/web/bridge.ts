import {
  SINGLE_ROOT_LIMIT_MESSAGE,
  type AgentEventEnvelope,
  type ApprovalEnvelope,
  type BuildState,
  type ChatMessage,
  type Citation,
  type Conversation,
  type ExecutionSummary,
  type FileNode,
  type LlmKeyStatus,
  type Root,
  type ScanPreview,
  type Session,
} from '@contracts';
import type { CodegateApi } from '../../../preload/api';

const SUPPORTED_EXTENSIONS = new Set(['hwp', 'hwpx', 'docx', 'pdf', 'md', 'txt']);
const SEEDED_ROOT = 'Folding 웹 데모';
const SEEDED_FILES = [
  '지원사업/지난달 제출/사업신청서.hwp',
  '지원사업/지난달 제출/사업계획서.pdf',
  '계약/송파하남선 광역철도 1공구 건설공사.pdf',
];

type Listener<T> = (value: T) => void;

interface WebFileHandle {
  kind: 'file';
  name: string;
  getFile(): Promise<File>;
}

interface WebDirectoryHandle {
  kind: 'directory';
  name: string;
  values(): AsyncIterableIterator<WebDirectoryHandle | WebFileHandle>;
}

interface BrowserRuntime {
  showDirectoryPicker?: () => Promise<WebDirectoryHandle>;
  open?: (url?: string | URL, target?: string) => Window | null;
  alert?: (message?: string) => void;
}

interface PreviewEntry {
  preview: ScanPreview;
  files: Map<string, WebFileHandle>;
}

export function installWebBridge(): void {
  if (typeof window === 'undefined' || window.codegate) return;
  document.documentElement.dataset.runtime = 'web-demo';
  window.codegate = createWebBridge();
}

export function createWebBridge(): CodegateApi {
  let session: Session = { authenticated: false };
  let roots: Root[] = [];
  let tree: FileNode[] = [];
  let build = idleBuild();
  let keyStatuses: LlmKeyStatus[] = [
    { provider: 'anthropic', configured: true, hint: '서버 관리' },
    { provider: 'gemini', configured: false },
    { provider: 'openai', configured: false },
  ];
  let sequence = 0;

  const previews = new Map<string, PreviewEntry>();
  const conversations: Conversation[] = [];
  const messages = new Map<string, ChatMessage[]>();
  const sessionListeners = new Set<Listener<Session>>();
  const treeListeners = new Set<Listener<FileNode[]>>();
  const buildListeners = new Set<Listener<BuildState>>();
  const agentListeners = new Set<Listener<AgentEventEnvelope>>();
  const approvalListeners = new Set<Listener<ApprovalEnvelope>>();
  const chatTimers = new Map<string, Set<ReturnType<typeof setTimeout>>>();

  const api = {
    auth: {
      async login(): Promise<Session> {
        session = {
          authenticated: true,
          email: 'demo@codegate.local',
          subjectId: 'web-demo-user',
          tenantId: 'web-demo',
          readAccess: ['documents'],
          writeScope: 'none',
          provisioned: true,
          authzSource: 'web-demo',
        };
        emit(sessionListeners, session);
        return session;
      },
      async logout(): Promise<void> {
        session = { authenticated: false };
        emit(sessionListeners, session);
      },
      async session(): Promise<Session> {
        return session;
      },
      onChanged(callback: Listener<Session>): () => void {
        return subscribe(sessionListeners, callback);
      },
    },

    roots: {
      async list(): Promise<Root[]> {
        return roots;
      },
      async pickDialog(): Promise<string | null> {
        const runtime = globalThis as typeof globalThis & BrowserRuntime;
        if (!runtime.showDirectoryPicker) {
          const seeded = seededPreview();
          previews.set(seeded.preview.root, seeded);
          return seeded.preview.root;
        }

        try {
          const handle = await runtime.showDirectoryPicker();
          const entry = await collectDirectory(handle);
          previews.set(entry.preview.root, entry);
          return entry.preview.root;
        } catch (error) {
          if (error instanceof DOMException && error.name === 'AbortError') return null;
          throw error;
        }
      },
      async scanPreview(path: string): Promise<ScanPreview> {
        const entry = previews.get(path);
        if (!entry) throw new Error('선택한 폴더를 다시 열어 주세요.');
        return entry.preview;
      },
      async add(path: string): Promise<Root> {
        const entry = previews.get(path);
        if (!entry) throw new Error('선택한 폴더를 다시 열어 주세요.');
        const active = roots[0];
        if (active) {
          if (active.path === path) return active;
          throw new Error(SINGLE_ROOT_LIMIT_MESSAGE);
        }
        const root: Root = {
          id: nextId('root'),
          path,
          includedCount: entry.preview.included.length,
          excludedCount: entry.preview.excluded.length,
          addedAt: new Date().toISOString(),
        };
        roots = [...roots.filter((item) => item.path !== path), root];
        tree = roots.flatMap((item) => {
          const selected = previews.get(item.path);
          return selected ? [directoryTree(item.path, selected.preview.included)] : [];
        });
        emit(treeListeners, tree);
        startDemoBuild();
        return root;
      },
      async remove(id: string): Promise<void> {
        roots = roots.filter((root) => root.id !== id);
        tree = roots.flatMap((item) => {
          const selected = previews.get(item.path);
          return selected ? [directoryTree(item.path, selected.preview.included)] : [];
        });
        emit(treeListeners, tree);
        if (roots.length > 0) {
          startDemoBuild();
        } else {
          build = {
            buildId: null,
            status: 'idle',
            progress: 0,
            message: '문서 폴더를 선택해 주세요',
            updatedAt: new Date().toISOString(),
          };
          emit(buildListeners, build);
        }
      },
    },

    build: {
      async trigger(): Promise<void> {
        startDemoBuild();
      },
      async state(): Promise<BuildState> {
        return build;
      },
      onChanged(callback: Listener<BuildState>): () => void {
        return subscribe(buildListeners, callback);
      },
    },

    llmKey: {
      async status(): Promise<LlmKeyStatus[]> {
        return keyStatuses;
      },
      async set(input): Promise<LlmKeyStatus[]> {
        keyStatuses = keyStatuses.map((status) =>
          status.provider === input.provider
            ? { provider: input.provider, configured: true, hint: `…${input.apiKey.slice(-4)}` }
            : status,
        );
        return keyStatuses;
      },
      async clear(provider): Promise<LlmKeyStatus[]> {
        keyStatuses = keyStatuses.map((status) =>
          status.provider === provider ? { provider, configured: false } : status,
        );
        return keyStatuses;
      },
    },

    tree: {
      async get(): Promise<FileNode[]> {
        return tree;
      },
      onChanged(callback: Listener<FileNode[]>): () => void {
        return subscribe(treeListeners, callback);
      },
    },

    chat: {
      async list(): Promise<Conversation[]> {
        return conversations;
      },
      async create(): Promise<Conversation> {
        const conversation: Conversation = {
          id: nextId('conversation'),
          title: conversations.length === 0 ? '문서 질문' : '새 대화',
          updatedAt: new Date().toISOString(),
        };
        conversations.unshift(conversation);
        messages.set(conversation.id, []);
        return conversation;
      },
      async messages(conversationId: string): Promise<ChatMessage[]> {
        return messages.get(conversationId) ?? [];
      },
      async send(conversationId: string, text: string): Promise<{ messageId: string }> {
        const messageId = nextId('assistant');
        const now = new Date().toISOString();
        const existing = messages.get(conversationId) ?? [];
        existing.push(
          {
            id: nextId('user'),
            conversationId,
            role: 'user',
            text,
            createdAt: now,
          },
          {
            id: messageId,
            conversationId,
            role: 'assistant',
            text: '',
            createdAt: now,
            streaming: true,
          },
        );
        messages.set(conversationId, existing);
        const conversation = conversations.find((item) => item.id === conversationId);
        if (conversation) {
          conversation.title = text.length > 22 ? `${text.slice(0, 22)}…` : text;
          conversation.updatedAt = now;
        }
        scheduleAnswer(conversationId, messageId, text);
        return { messageId };
      },
      async abort(conversationId: string): Promise<void> {
        clearConversationTimers(conversationId);
        const current = messages.get(conversationId) ?? [];
        for (const message of current) message.streaming = false;
      },
      onAgentEvent(callback: Listener<AgentEventEnvelope>): () => void {
        return subscribe(agentListeners, callback);
      },
    },

    execution: {
      async retry(_messageId: string, executionId: string): Promise<ExecutionSummary> {
        return demoExecution(executionId, 'completed');
      },
      async undo(_messageId: string, executionId: string): Promise<ExecutionSummary> {
        return demoExecution(executionId, 'undone');
      },
    },

    approval: {
      onRequest(_callback): () => void {
        return subscribe(approvalListeners, _callback);
      },
      async respond(): Promise<void> {},
    },

    wiki: {
      /**
       * 웹 데모에는 위키 빌드가 없다 — 그래프를 만들 재료 자체가 없다.
       * 가짜 그래프를 그리느니 **비어 있다고 말한다**. 화면은 빈 상태를 안내로 처리한다.
       */
      async graph() {
        return { buildId: null, nodes: [], edges: [] };
      },
    },

    document: {
      /**
       * 웹 데모는 사용자의 디스크에 파일을 만들지 않는다.
       * 되는 척하지 않고, 어디서 되는지 말한다.
       */
      async fillTemplate(): Promise<string | null> {
        (globalThis as typeof globalThis & BrowserRuntime).alert?.(
          '양식 채우기는 데스크톱 앱에서만 됩니다.',
        );
        return null;
      },
      async saveHwpx(): Promise<string | null> {
        (globalThis as typeof globalThis & BrowserRuntime).alert?.(
          '한글 문서로 저장은 데스크톱 앱에서만 됩니다.',
        );
        return null;
      },
    },

    shell: {
      async openOriginal(relativePath: string): Promise<void> {
        for (const entry of previews.values()) {
          const handle = entry.files.get(relativePath);
          if (!handle) continue;
          const file = await handle.getFile();
          const url = URL.createObjectURL(file);
          (globalThis as typeof globalThis & BrowserRuntime).open?.(url, '_blank');
          setTimeout(() => URL.revokeObjectURL(url), 30_000);
          return;
        }
        (globalThis as typeof globalThis & BrowserRuntime).alert?.(
          '샘플 문서는 웹 데모에 포함된 가상 파일입니다.',
        );
      },
      /**
       * 브라우저는 파일 탐색기를 열 수 없다 — 샌드박스 밖이다.
       * 되는 척하지 않고, 왜 안 되는지 말한다.
       */
      async revealOriginal(): Promise<void> {
        (globalThis as typeof globalThis & BrowserRuntime).alert?.(
          '폴더에서 보기는 데스크톱 앱에서만 됩니다. 브라우저는 파일 탐색기를 열 수 없어요.',
        );
      },
    },
  } satisfies CodegateApi;

  return api;

  function nextId(prefix: string): string {
    sequence += 1;
    return `${prefix}-web-${sequence}`;
  }

  function startDemoBuild(): void {
    const updates: Array<[number, BuildState]> = [
      [
        0,
        {
          buildId: 'knowledge-sync',
          status: 'converting',
          progress: 16,
          phase: { name: 'convert', done: 1, total: 3 },
          message: '브라우저에서 문서 목록을 정리하는 중…',
          updatedAt: new Date().toISOString(),
        },
      ],
      [
        350,
        {
          buildId: 'knowledge-sync',
          status: 'enriching',
          progress: 58,
          phase: { name: 'enrich', done: 2, total: 3 },
          message: '웹 데모 인덱스를 준비하는 중…',
          updatedAt: new Date().toISOString(),
        },
      ],
      [
        700,
        {
          buildId: 'knowledge-sync',
          status: 'assembling',
          progress: 86,
          phase: { name: 'assemble', done: 2, total: 3 },
          message: '문서 트리를 조립하는 중…',
          updatedAt: new Date().toISOString(),
        },
      ],
      [
        1_000,
        {
          buildId: 'knowledge-sync',
          status: 'done',
          progress: 100,
          phase: { name: 'assemble', done: 3, total: 3 },
          message: '웹 데모 준비가 끝났습니다.',
          updatedAt: new Date().toISOString(),
        },
      ],
    ];
    for (const [delay, state] of updates) {
      setTimeout(() => {
        build = state;
        emit(buildListeners, build);
      }, delay);
    }
  }

  function scheduleAnswer(conversationId: string, messageId: string, question: string): void {
    clearConversationTimers(conversationId);
    const citations = answerCitations(question);
    const answer = composeDemoAnswer(question);
    const timers = new Set<ReturnType<typeof setTimeout>>();
    chatTimers.set(conversationId, timers);
    schedule(100, { type: 'tool_start', name: 'wiki_search', summary: '등록 문서에서 근거 검색' });
    schedule(340, { type: 'text_delta', text: answer });
    schedule(520, { type: 'done', citations });

    function schedule(delay: number, event: AgentEventEnvelope['event']): void {
      const timer = setTimeout(() => {
        timers.delete(timer);
        const envelope = { conversationId, messageId, event };
        const assistant = (messages.get(conversationId) ?? []).find((item) => item.id === messageId);
        if (assistant && event.type === 'text_delta') assistant.text += event.text;
        if (assistant && event.type === 'done') {
          assistant.citations = event.citations;
          assistant.streaming = false;
        }
        emit(agentListeners, envelope);
      }, delay);
      timers.add(timer);
    }
  }

  function clearConversationTimers(conversationId: string): void {
    const timers = chatTimers.get(conversationId);
    if (!timers) return;
    for (const timer of timers) clearTimeout(timer);
    chatTimers.delete(conversationId);
  }
}

function subscribe<T>(listeners: Set<Listener<T>>, callback: Listener<T>): () => void {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

function emit<T>(listeners: Set<Listener<T>>, value: T): void {
  for (const listener of listeners) listener(value);
}

function idleBuild(): BuildState {
  return {
    buildId: null,
    status: 'idle',
    progress: 0,
    message: '폴더를 선택해 주세요.',
    updatedAt: new Date().toISOString(),
  };
}

function seededPreview(): PreviewEntry {
  return {
    preview: { root: SEEDED_ROOT, included: SEEDED_FILES, excluded: [] },
    files: new Map(),
  };
}

async function collectDirectory(root: WebDirectoryHandle): Promise<PreviewEntry> {
  const included: string[] = [];
  const excluded: ScanPreview['excluded'] = [];
  const files = new Map<string, WebFileHandle>();

  await visit(root, '');
  return {
    preview: { root: root.name, included: included.sort(), excluded: excluded.sort(byPath) },
    files,
  };

  async function visit(directory: WebDirectoryHandle, prefix: string): Promise<void> {
    for await (const handle of directory.values()) {
      const relativePath = prefix ? `${prefix}/${handle.name}` : handle.name;
      if (handle.kind === 'directory') {
        await visit(handle, relativePath);
        continue;
      }
      const extension = handle.name.split('.').pop()?.toLowerCase() ?? '';
      if (!SUPPORTED_EXTENSIONS.has(extension)) {
        excluded.push({ path: relativePath, reason: '지원하지 않는 형식' });
        continue;
      }
      included.push(relativePath);
      files.set(relativePath, handle);
    }
  }
}

function byPath(left: { path: string }, right: { path: string }): number {
  return left.path.localeCompare(right.path, 'ko');
}

function directoryTree(rootName: string, paths: string[]): FileNode {
  const root: FileNode = {
    path: rootName,
    name: rootName,
    isDir: true,
    status: 'done',
    children: [],
  };
  for (const relativePath of paths) {
    const parts = relativePath.split('/').filter(Boolean);
    let parent = root;
    parts.forEach((part, index) => {
      const isDir = index < parts.length - 1;
      const path = [rootName, ...parts.slice(0, index + 1)].join('/');
      let child = parent.children?.find((item) => item.name === part);
      if (!child) {
        child = { path, name: part, isDir, status: 'done', children: isDir ? [] : undefined };
        parent.children?.push(child);
      }
      parent = child;
    });
  }
  return root;
}

function composeDemoAnswer(question: string): string {
  if (/(수정|삭제|삽입|바꿔|고쳐|만들어)/.test(question)) {
    return '웹 데모에서는 원본 변경을 실행하지 않습니다. 데스크톱 앱에서만 승인과 백업을 거쳐 변경할 수 있어요.';
  }
  if (/(지난달|신청서|요약)/.test(question)) {
    return [
      '지난달 제출한 신청서는 AI 기반 바이오 소재 융복합 제품의 적용·검증과 시장 진출 지원을 신청한 문서입니다.',
      '',
      '신청 기업의 기본 정보와 사업자등록번호, 기업 유형, 주생산품이 포함되어 있으며 사업계획서에는 지원 목적과 추진 내용이 정리되어 있습니다.',
      '',
      '별도로 등록된 송파하남선 건설공사 문서는 이번 신청서 요약에서 제외했습니다.',
    ].join('\n');
  }
  if (/(콜라겐|사업계획)/.test(question)) {
    return '사업계획서에는 바이오 소재 제품의 적용 가능성과 검증 계획이 핵심 근거로 정리되어 있습니다. 시장 진출 주장은 제품 검증 일정과 사업화 계획을 함께 확인해야 합니다.';
  }
  return '등록된 문서에서 질문과 직접 관련된 근거를 찾았습니다. 웹 데모는 화면 흐름과 인용 표시를 보여주며, 실제 문서 내용 분석은 데스크톱 앱에서 수행됩니다.';
}

function answerCitations(question: string): Citation[] {
  if (/(지난달|신청서|요약|콜라겐|사업계획)/.test(question)) {
    return [
      {
        docId: 'FORM-1',
        rev: 1,
        section: '사업 신청 프로그램',
        sectionId: '사업 신청 프로그램',
        sourcePath: '지원사업/지난달 제출/사업신청서.hwp',
      },
      {
        docId: 'PLAN-1',
        rev: 1,
        section: '사업 개요',
        sectionId: '사업 개요',
        sourcePath: '지원사업/지난달 제출/사업계획서.pdf',
      },
    ];
  }
  return [];
}

function demoExecution(executionId: string, status: string): ExecutionSummary {
  return {
    executionId,
    documentId: 'web-demo',
    status,
    stage: status,
    terminal: true,
    canRetry: false,
    canUndo: false,
  };
}
