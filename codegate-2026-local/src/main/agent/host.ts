/**
 * 채팅 호스트 — 에이전트 스트림을 IPC 로 중계하고 SQLite 에 적재한다 (스펙 v1.3 §1 L1 · §2).
 * 스트리밍 도중에도 텍스트/인용을 계속 저장하므로 앱을 껐다 켜도 히스토리가 남는다.
 */
import { randomUUID } from 'node:crypto';
import {
  AUTH_ERROR_MESSAGE,
  AuthError,
  type Agent,
  type AgentDeps,
  type AgentEvent,
  type AgentEventEnvelope,
  type ChatMessage,
  type Citation,
  type Conversation,
  type CreateAgent,
  type ExecutionSummary,
} from '@contracts';
import type { Store } from '@main/db/store';
import { logError, toUserMessage } from '@main/util/errors';

export interface ChatServiceOptions {
  store: Store;
  deps: AgentDeps;
  createAgent: CreateAgent;
  /** 렌더러로 `IPC_EVENTS.agentEvent` 전송 */
  sendEvent: (envelope: AgentEventEnvelope) => void;
  /** 401/402 를 세션에 반영하기 위한 훅 */
  onAuthError?: (error: AuthError) => void | Promise<void>;
  rejectApprovals?: () => void;
  deleteRemoteConversation?: (conversationId: string) => Promise<void>;
}

/** DB 쓰기 폭주를 막기 위한 부분 저장 간격. */
const PERSIST_INTERVAL_MS = 250;

export class ChatService {
  private readonly agent: Agent;
  private readonly aborters = new Map<string, AbortController>();

  constructor(private readonly options: ChatServiceOptions) {
    this.agent = options.createAgent(options.deps);
  }

  get busy(): boolean {
    return this.aborters.size > 0;
  }

  list(): Conversation[] {
    return this.options.store.listConversations();
  }

  create(): Conversation {
    const now = new Date().toISOString();
    const conversation: Conversation = { id: randomUUID(), title: '새 채팅', updatedAt: now };
    this.options.store.createConversation(conversation);
    return conversation;
  }

  messages(conversationId: string): ChatMessage[] {
    return this.options.store.listMessages(conversationId);
  }

  async delete(conversationId: string): Promise<void> {
    if (!this.options.store.getConversation(conversationId)) return;
    this.abort(conversationId);
    await this.options.deleteRemoteConversation?.(conversationId);
    this.options.store.deleteConversation(conversationId);
  }

  /** 전송만 하고 즉시 반환한다 — 응답은 `IPC_EVENTS.agentEvent` 로 스트리밍된다. */
  send(conversationId: string, text: string): { messageId: string } {
    const store = this.options.store;
    if (!store.getConversation(conversationId)) {
      const now = new Date().toISOString();
      store.createConversation({ id: conversationId, title: '새 채팅', updatedAt: now });
    }

    const now = new Date().toISOString();
    store.insertMessage({
      id: randomUUID(),
      conversationId,
      role: 'user',
      text,
      createdAt: now,
    });

    const existing = store.listMessages(conversationId);
    if (existing.filter((m) => m.role === 'user').length === 1) {
      store.touchConversation(conversationId, titleFrom(text));
    } else {
      store.touchConversation(conversationId);
    }

    const messageId = randomUUID();
    store.insertMessage({
      id: messageId,
      conversationId,
      role: 'assistant',
      text: '',
      createdAt: new Date().toISOString(),
      streaming: true,
    });

    void this.stream(conversationId, messageId, text);
    return { messageId };
  }

  /** 스트리밍 중단 — `AbortController` 로 루프를 빠져나온다. */
  abort(conversationId: string): void {
    this.aborters.get(conversationId)?.abort();
    this.options.rejectApprovals?.();
  }

  abortAll(): void {
    for (const controller of this.aborters.values()) controller.abort();
    this.aborters.clear();
    this.options.rejectApprovals?.();
  }

  private async stream(conversationId: string, messageId: string, text: string): Promise<void> {
    const store = this.options.store;
    const controller = new AbortController();
    this.aborters.set(conversationId, controller);

    let buffer = '';
    let citations: Citation[] = [];
    let execution: ExecutionSummary | undefined;
    /**
     * 승인 요청이 가리킨 문서. 답변에 "열기" 를 붙이기 위해 들고 있는다.
     * 승인 결과는 이벤트 스트림에 없으므로(에이전트가 내부에서 처리한다) 여기서는
     * **대상만** 기억하고, 실제로 반영됐는지는 답변 본문·execution 이 말하게 둔다.
     */
    let changedPath: string | undefined;
    const tools: { name: string; summary: string }[] = [];
    let lastPersist = 0;

    const emit = (event: AgentEvent): void => {
      this.options.sendEvent({ conversationId, messageId, event });
    };

    /** `force` 는 스로틀만 건너뛴다 — streaming 플래그는 항상 true (종료는 finally 에서). */
    const persist = (force = false): void => {
      const now = Date.now();
      if (!force && now - lastPersist < PERSIST_INTERVAL_MS) return;
      lastPersist = now;
      store.updateMessage(messageId, { text: buffer, tools, citations, streaming: true });
    };

    try {
      for await (const event of this.agent.send(text, {
        conversationId,
        signal: controller.signal,
      })) {
        if (controller.signal.aborted) break;

        if (event.type === 'text_delta') {
          buffer += event.text;
          persist();
        } else if (event.type === 'tool_start') {
          tools.push({ name: event.name, summary: event.summary });
          persist(true);
        } else if (event.type === 'approval_request') {
          if (event.target) changedPath = event.target;
        } else if (event.type === 'done') {
          citations = event.citations;
          execution = event.execution;
        } else if (event.type === 'auth_error') {
          // 401/402 는 절대 삼키지 않는다 (스펙 v1.3 §5)
          buffer += `\n\n${AUTH_ERROR_MESSAGE[event.kind]}`;
          await this.options.onAuthError?.(new AuthError(event.kind, AUTH_ERROR_MESSAGE[event.kind]));
        }
        emit(event);
      }

      if (controller.signal.aborted) {
        buffer += '\n\n(사용자가 응답 생성을 중단했습니다.)';
        emit({ type: 'done', citations });
      }
    } catch (err) {
      if (controller.signal.aborted || (err instanceof Error && err.name === 'AbortError')) {
        buffer += '\n\n(사용자가 응답 생성을 중단했습니다.)';
        emit({ type: 'done', citations, execution });
        return;
      }
      logError('chat', err);
      if (err instanceof AuthError) {
        await this.options.onAuthError?.(err);
        emit({ type: 'auth_error', kind: err.kind });
        buffer += `\n\n${err.message}`;
      } else {
        buffer += `\n\n${toUserMessage(err, '답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.')}`;
      }
      emit({ type: 'done', citations });
    } finally {
      this.aborters.delete(conversationId);
      store.updateMessage(messageId, {
        text: buffer,
        tools,
        citations,
        execution,
        changedPath,
        streaming: false,
      });
      store.touchConversation(conversationId);
    }
  }
}

function titleFrom(text: string): string {
  const oneLine = text.replace(/\s+/g, ' ').trim();
  return oneLine.length > 30 ? `${oneLine.slice(0, 30)}…` : oneLine || '새 채팅';
}
