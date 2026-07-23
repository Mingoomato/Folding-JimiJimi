import { describe, expect, it } from 'vitest';
import { Store, type Db } from '@main/db/store';

describe('chat deletion', () => {
  it('선택한 대화와 메시지만 원자적으로 삭제한다', () => {
    const store = new Store(fakeDb());

    try {
      store.deleteConversation('conversation-1');
      store.deleteConversation('conversation-1');

      expect(store.getConversation('conversation-1')).toBeNull();
      expect(store.listMessages('conversation-1')).toEqual([]);
      expect(store.getConversation('conversation-2')).toEqual({
        id: 'conversation-2',
        title: '보존할 채팅',
        updatedAt: '2026-07-23T01:00:00.000Z',
      });
      expect(store.listMessages('conversation-2')).toEqual([
        {
          id: 'message-2',
          conversationId: 'conversation-2',
          role: 'user',
          text: '보존할 내용',
          createdAt: '2026-07-23T02:00:00.000Z',
        },
      ]);
    } finally {
      store.close();
    }
  });
});

function fakeDb(): Db {
  let conversations = [
    {
      id: 'conversation-1',
      title: '삭제할 채팅',
      updated_at: '2026-07-23T00:00:00.000Z',
    },
    {
      id: 'conversation-2',
      title: '보존할 채팅',
      updated_at: '2026-07-23T01:00:00.000Z',
    },
  ];
  let messages = [
    {
      id: 'message-1',
      conversation_id: 'conversation-1',
      role: 'user',
      text: '삭제할 내용',
      citations: null,
      tools: null,
      execution: null,
      changed_path: null,
      streaming: 0,
      created_at: '2026-07-23T02:00:00.000Z',
    },
    {
      id: 'message-2',
      conversation_id: 'conversation-2',
      role: 'user',
      text: '보존할 내용',
      citations: null,
      tools: null,
      execution: null,
      changed_path: null,
      streaming: 0,
      created_at: '2026-07-23T02:00:00.000Z',
    },
  ];

  const db = {
    prepare(sql: string) {
      return {
        run(conversationId: string) {
          if (sql.includes('DELETE FROM messages')) {
            messages = messages.filter((message) => message.conversation_id !== conversationId);
          }
          if (sql.includes('DELETE FROM conversations')) {
            conversations = conversations.filter(
              (conversation) => conversation.id !== conversationId,
            );
          }
        },
        get(conversationId: string) {
          const conversation = conversations.find((item) => item.id === conversationId);
          if (!conversation) return undefined;
          return {
            id: conversation.id,
            title: conversation.title,
            updatedAt: conversation.updated_at,
          };
        },
        all(conversationId: string) {
          return messages.filter((message) => message.conversation_id === conversationId);
        },
      };
    },
    transaction<TArgs extends unknown[]>(fn: (...args: TArgs) => void) {
      return (...args: TArgs) => fn(...args);
    },
    close() {},
  };
  return db as unknown as Db;
}
