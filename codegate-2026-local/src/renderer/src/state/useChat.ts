import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatMessage, Conversation } from '@contracts';
import { AUTH_ERROR_MESSAGE } from '@contracts';

let localSeq = 0;
const localId = (p: string) => `${p}-local-${++localSeq}`;

/**
 * L1 — 채팅. 메인에서 오는 AgentEvent 스트림을 메시지로 누적한다.
 *
 * 메인이 SQLite에 영속화하므로 여기 상태는 화면용 캐시다.
 * 대화를 바꾸면 메인에서 다시 읽어온다.
 */
export function useChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  /** 401/402 — 사용자 문장으로 배너에 띄운다 (스펙 v1.3 §5, silent fail 금지) */
  const [authError, setAuthError] = useState<string | null>(null);

  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;

  // 대화 목록 최초 로드 — 없으면 하나 만든다.
  useEffect(() => {
    let alive = true;
    (async () => {
      const list = await window.codegate.chat.list();
      if (!alive) return;
      if (list.length === 0) {
        const created = await window.codegate.chat.create();
        if (!alive) return;
        setConversations([created]);
        setActiveId(created.id);
      } else {
        setConversations(list);
        setActiveId(list[0].id);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 활성 대화가 바뀌면 메시지를 다시 읽는다.
  useEffect(() => {
    if (!activeId) return;
    let alive = true;
    window.codegate.chat.messages(activeId).then((m) => alive && setMessages(m));
    return () => {
      alive = false;
    };
  }, [activeId]);

  // AgentEvent 구독 — 스트리밍 누적의 핵심.
  useEffect(() => {
    return window.codegate.chat.onAgentEvent(({ conversationId, messageId, event }) => {
      // 다른 대화의 이벤트는 무시한다 (메인이 이미 영속화했다).
      if (conversationId !== activeIdRef.current) return;

      setMessages((prev) => {
        const i = prev.findIndex((m) => m.id === messageId);
        if (i === -1) return prev;
        const msg = { ...prev[i] };
        const next = [...prev];

        switch (event.type) {
          case 'text_delta':
            msg.text += event.text;
            break;
          case 'tool_start':
            msg.tools = [...(msg.tools ?? []), { name: event.name, summary: event.summary }];
            break;
          case 'done':
            msg.citations = event.citations;
            msg.execution = event.execution;
            msg.streaming = false;
            break;
          case 'auth_error':
          case 'approval_request':
            return prev; // 아래 별도 처리
        }
        next[i] = msg;
        return next;
      });

      if (event.type === 'done') setStreaming(false);
      if (event.type === 'auth_error') {
        setAuthError(AUTH_ERROR_MESSAGE[event.kind]);
        setStreaming(false);
        setMessages((prev) =>
          prev.map((m) => (m.id === messageId ? { ...m, streaming: false } : m)),
        );
      }
    });
  }, []);

  const send = useCallback(
    async (text: string) => {
      const conversationId = activeIdRef.current;
      if (!conversationId || !text.trim() || streaming) return;
      setAuthError(null);
      setStreaming(true);

      const now = new Date().toISOString();
      const userMsg: ChatMessage = {
        id: localId('u'),
        conversationId,
        role: 'user',
        text,
        createdAt: now,
      };
      setMessages((prev) => [...prev, userMsg]);

      try {
        const { messageId } = await window.codegate.chat.send(conversationId, text);
        setMessages((prev) => [
          ...prev,
          {
            id: messageId,
            conversationId,
            role: 'assistant',
            text: '',
            createdAt: new Date().toISOString(),
            streaming: true,
          },
        ]);
      } catch (e) {
        setStreaming(false);
        setAuthError(e instanceof Error ? e.message : '답변을 가져오지 못했습니다.');
      }
    },
    [streaming],
  );

  const abort = useCallback(async () => {
    if (!activeIdRef.current) return;
    await window.codegate.chat.abort(activeIdRef.current);
    setStreaming(false);
    setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
  }, []);

  const createConversation = useCallback(async () => {
    const c = await window.codegate.chat.create();
    setConversations((prev) => [c, ...prev]);
    setActiveId(c.id);
    setMessages([]);
  }, []);

  return {
    conversations,
    activeId,
    setActiveId,
    messages,
    streaming,
    authError,
    dismissAuthError: () => setAuthError(null),
    send,
    abort,
    createConversation,
  };
}
