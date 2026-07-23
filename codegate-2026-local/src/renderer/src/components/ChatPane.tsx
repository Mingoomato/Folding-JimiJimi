import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { AlertTriangle, ArrowUp, Square, X } from 'lucide-react';
import type { BuildState, ChatMessage } from '@contracts';
import { BUILD_PHASE_LABEL } from '@contracts';
import type { useChat } from '@/state/useChat';
import { cn } from '@/lib/cn';
import { MessageBubble } from './MessageBubble';
import { Mark } from '@/ui/Wordmark';

type Chat = ReturnType<typeof useChat>;

const EXAMPLES = [
  '사업계획서의 콜라겐 주장 근거가 뭐야?',
  '계약서 양식으로 자문계약서 만들어줘',
  '지난달 제출한 신청서 요약해줘',
];

/** 중앙 패널 — 채팅. 스트리밍 · 인용 · 도구 배지. */
export function ChatPane({ chat, build }: { chat: Chat; build: BuildState | null }) {
  const [draft, setDraft] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  /*
   * **문서 준비 상태로 채팅을 막지 않는다.**
   *
   * 예전에는 `status !== 'done'` 이면 입력을 잠갔는데, 실패(`failed`)도 여기 걸려서
   * 배너는 "준비 실패", 입력창은 "준비하는 중"이라고 말하는 모순이 생겼고 되돌릴 길이
   * 없는 막다른 길이 됐다. 위키 빌드는 불변이라 준비 중에도 직전 빌드로 답할 수 있고,
   * 근거가 없으면 에이전트가 "위키에서 확인되지 않음"이라고 말한다.
   */
  const knowledgeSyncing =
    build?.buildId === 'knowledge-sync' && build.status !== 'done' && build.status !== 'failed';
  const buildFailed = build?.status === 'failed';
  const building = build && build.status !== 'idle' && build.status !== 'done' && !buildFailed;

  // 새 내용이 붙으면 바닥으로 따라간다.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.messages]);

  function submit() {
    const text = draft.trim();
    if (!text || chat.streaming) return;
    setDraft('');
    void chat.send(text);
    if (taRef.current) taRef.current.style.height = 'auto';
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  return (
    // `h-full` 이 필요하다 — 예전에는 3분할 grid 의 직접 자식이라 자동으로 늘어났지만,
    // 탭이 생기며 래퍼 안으로 들어가면서 내용 높이만 차지하게 됐다(하단에 빈 공간).
    <main className="flex h-full min-h-0 min-w-0 flex-col bg-white">
      <div className="h-4 shrink-0" />

      {/* 빌드 중에도 답할 수 있다는 걸 알린다 (스펙 v1.3 §3) */}
      {(building || buildFailed) && (
        <div
          className={cn(
            'mx-6 mb-3 flex items-center gap-2.5 rounded-md px-3.5 py-2.5 text-xs',
            buildFailed ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700',
          )}
        >
          {!buildFailed && (
          <span className="relative flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-70" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
          </span>
          )}
          <span className="font-mono font-semibold">
            {buildFailed
              ? '준비 실패'
              : `${build.phase ? BUILD_PHASE_LABEL[build.phase.name] : '정리'} ${Math.round(build.progress)}%`}
          </span>
          <span className={buildFailed ? 'text-red-600' : 'text-blue-600'}>
            —{' '}
            {/* 실패해도 질문은 받는다 — 그 사실을 문장으로 분명히 말한다. */}
            {buildFailed
              ? `${build.error ?? '검색 엔진을 준비하지 못했습니다.'} 지금까지 정리된 범위에서는 답변할 수 있습니다.`
              : '지금까지 정리된 범위에서 답변합니다'}
          </span>
        </div>
      )}

      {/* 401/402 등 — 반드시 사용자 문장으로 (스펙 v1.3 §5) */}
      {chat.authError && (
        <div
          role="alert"
          className="mx-6 mb-3 flex items-start gap-2.5 rounded-md border border-amber-100 bg-amber-50 px-3.5 py-3 text-xs text-amber-600"
        >
          <AlertTriangle size={15} className="mt-px shrink-0" />
          <span className="flex-1">{chat.authError}</span>
          <button
            onClick={chat.dismissAuthError}
            className="shrink-0 text-amber-600/70 hover:text-amber-600"
            aria-label="닫기"
          >
            <X size={14} />
          </button>
        </div>
      )}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto px-6">
        {chat.messages.length === 0 ? (
          <EmptyState onPick={(t) => setDraft(t)} />
        ) : (
          <div className="mx-auto flex max-w-[760px] flex-col gap-7 pb-8">
            {chat.messages.map((m, index) => (
              <MessageBubble
                key={m.id}
                message={m}
                userQuery={m.role === 'assistant' ? precedingUserQuery(chat.messages, index) : undefined}
              />
            ))}
          </div>
        )}
      </div>

      {/* 입력 */}
      <div className="shrink-0 px-6 pb-6 pt-2">
        <div className="mx-auto max-w-[760px]">
          <div
            className={cn(
              'flex items-end gap-2 rounded-xl border border-ink-150 bg-white p-2.5 pl-4',
              'shadow-xs transition-[border-color,box-shadow] duration-[--dur-fast] ease-[--ease-standard]',
              'focus-within:border-blue-500 focus-within:shadow-[var(--focus-ring)]',
            )}
          >
            <textarea
              ref={taRef}
              rows={1}
              value={draft}
              placeholder={
                knowledgeSyncing
                  ? '준비 중 — 지금까지 정리된 범위에서 답합니다'
                  : '문서에 대해 물어보세요'
              }
              onChange={(e) => {
                setDraft(e.target.value);
                e.target.style.height = 'auto';
                e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
              }}
              onKeyDown={onKeyDown}
              className="max-h-[180px] min-h-[26px] flex-1 resize-none bg-transparent py-1 text-[15px] leading-normal text-ink-900 outline-none placeholder:text-ink-400"
            />
            {chat.streaming ? (
              <button
                onClick={() => void chat.abort()}
                aria-label="중지"
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-ink-100 text-ink-600 transition-colors hover:bg-ink-150 fold-focus"
              >
                <Square size={14} fill="currentColor" />
              </button>
            ) : (
              <button
                onClick={submit}
                disabled={!draft.trim()}
                aria-label="보내기"
                className={cn(
                  'flex h-9 w-9 shrink-0 items-center justify-center rounded-md transition-all duration-[--dur-fast] fold-focus',
                  draft.trim()
                    ? 'bg-blue-500 text-white shadow-brand-sm hover:bg-blue-600 active:translate-y-[0.5px] active:bg-blue-700'
                    : 'cursor-not-allowed bg-ink-100 text-ink-300',
                )}
              >
                <ArrowUp size={17} />
              </button>
            )}
          </div>
          <p className="mt-2 text-center text-2xs text-ink-400">
            답변은 위키에 있는 내용만 근거로 합니다. 확인되지 않으면 그렇게 말합니다.
          </p>
        </div>
      </div>
    </main>
  );
}

function precedingUserQuery(messages: readonly ChatMessage[], index: number): string | undefined {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    if (messages[cursor]?.role === 'user') return messages[cursor]?.text;
  }
  return undefined;
}

function EmptyState({ onPick }: { onPick: (t: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-[560px] flex-col items-center justify-center gap-6 pb-16 text-center">
      <Mark size={44} />
      <div>
        <h2 className="text-h3 font-bold tracking-tight text-ink-950">무엇을 찾아드릴까요?</h2>
        <p className="mt-2 text-sm text-ink-500">
          등록한 폴더의 문서에서 근거를 찾아 답합니다.
        </p>
      </div>
      <ul className="flex w-full flex-col gap-2">
        {EXAMPLES.map((e) => (
          <li key={e}>
            <button
              onClick={() => onPick(e)}
              className="w-full rounded-md border border-ink-100 bg-white px-4 py-3 text-left text-[13px] text-ink-600 shadow-xs transition-all duration-[--dur-fast] ease-[--ease-standard] hover:-translate-y-0.5 hover:border-ink-150 hover:shadow-md fold-focus"
            >
              {e}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
