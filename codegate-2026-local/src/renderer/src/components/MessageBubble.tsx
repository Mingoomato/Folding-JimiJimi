import { useEffect, useState } from 'react';
import type { ChatMessage } from '@contracts';
import { NOT_FOUND_IN_WIKI } from '@contracts';
import { Mark } from '@/ui/Wordmark';
import { Markdown } from './Markdown';
import { ToolBadge } from './ToolBadge';
import { CitationChip } from './CitationChip';

/**
 * 한 메시지.
 *   사용자 — 우측 정렬 ink 버블 (브랜드 블루는 인용·액션에만 남겨 둔다)
 *   에이전트 — 전폭 본문 + 도구 배지 + 인용 칩
 */
export function MessageBubble({
  message,
  userQuery,
}: {
  message: ChatMessage;
  userQuery?: string;
}) {
  const [execution, setExecution] = useState(message.execution);
  const [action, setAction] = useState<'retry' | 'undo' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [savingHwpx, setSavingHwpx] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedPath, setSavedPath] = useState<string | null>(null);

  useEffect(() => setExecution(message.execution), [message.execution]);

  async function runExecutionAction(kind: 'retry' | 'undo') {
    if (!execution || action) return;
    setAction(kind);
    setActionError(null);
    try {
      const next = await window.codegate.execution[kind](message.id, execution.executionId);
      setExecution(next);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '실행 상태를 변경하지 못했습니다.');
    } finally {
      setAction(null);
    }
  }

  async function saveAsHwpx() {
    if (savingHwpx || !message.text) return;
    setSavingHwpx(true);
    setSaveError(null);
    try {
      const template = message.citations?.find((citation) =>
        citation.sourcePath?.toLowerCase().endsWith('.hwp'),
      );
      setSavedPath(
        await window.codegate.document.saveHwpx(message.text, template?.sourcePath, userQuery),
      );
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : '한글 문서를 저장하지 못했습니다.');
    } finally {
      setSavingHwpx(false);
    }
  }

  // 메시지가 툭 나타나지 않고 아래에서 서서히 올라오게 한다.
  // prefers-reduced-motion 은 base.css 에서 전역으로 꺼진다.
  const enter = { animation: 'fold-message-in 320ms var(--ease-out) both' };

  if (message.role === 'user') {
    return (
      <div className="flex justify-end" style={enter}>
        <div
          data-selectable
          className="max-w-[80%] whitespace-pre-wrap break-words rounded-xl bg-ink-100 px-4 py-2.5 text-[15px] leading-normal text-ink-900"
        >
          {message.text}
        </div>
      </div>
    );
  }

  const unresolved = message.text.includes(NOT_FOUND_IN_WIKI);

  return (
    <div className="flex gap-3.5" style={enter}>
      <Mark size={26} className="mt-0.5" />

      <div className="min-w-0 flex-1">
        {message.tools && message.tools.length > 0 && (
          <div className="mb-3 flex flex-wrap gap-1.5">
            {message.tools.map((t, i) => (
              <ToolBadge key={`${t.name}-${i}`} name={t.name} summary={t.summary} />
            ))}
          </div>
        )}

        {message.text ? (
          // 답변은 마크다운으로 렌더링한다 — 위키 원문에 표·제목·목록이 섞여 오기 때문이다.
          // 캐럿은 마크다운 블록 **밖에** 둔다. 안에 넣으면 스트리밍 도중 문단이 끊길 때마다
          // 위치가 튀고, 미완성 마크다운 안에 끼어 렌더링이 흔들린다.
          <div
            data-selectable
            className="break-words text-[15px] leading-relaxed text-ink-800"
          >
            <Markdown>{message.text}</Markdown>
            {message.streaming && (
              <span
                className="ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[0.18em] bg-blue-500 align-baseline"
                style={{ animation: 'fold-caret 1s steps(1) infinite' }}
              />
            )}
          </div>
        ) : (
          message.streaming && <ThinkingDots />
        )}

        {unresolved && (
          <p className="mt-3 rounded-md border border-amber-100 bg-amber-50 px-3 py-2 text-xs text-amber-600">
            위키에 근거가 없어 답을 확정하지 못했습니다. 폴더에 관련 문서가 있는지 확인해 주세요.
          </p>
        )}

        {message.citations && message.citations.length > 0 && (
          <div className="mt-3.5 flex flex-wrap gap-1.5">
            {message.citations.map((c, i) => (
              <CitationChip key={`${c.docId}-${c.section}-${i}`} citation={c} />
            ))}
          </div>
        )}

        {!message.streaming &&
          message.text &&
          !unresolved &&
          message.citations &&
          message.citations.length > 0 && (
          <div className="mt-3.5 flex flex-wrap items-center gap-2 text-xs">
            <button
              disabled={savingHwpx}
              onClick={() => void saveAsHwpx()}
              className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 font-semibold text-blue-700 hover:bg-blue-100 disabled:opacity-50"
            >
              {savingHwpx ? '미리보기 준비 중…' : 'HWPX로 저장'}
            </button>
            {savedPath && <span className="text-green-700">승인된 문서를 생성했습니다.</span>}
            {saveError && <span className="text-red-600">{saveError}</span>}
          </div>
        )}

        {message.changedPath && !message.streaming && (
          <ChangedFileBar path={message.changedPath} />
        )}

        {execution && (
          <div className="mt-3.5 rounded-md border border-ink-100 bg-ink-50 px-3.5 py-3 text-xs text-ink-600">
            <div className="flex items-center gap-2">
              <span className="font-mono text-2xs text-ink-400">{execution.stage}</span>
              <span className="flex-1">
                {execution.error?.message ??
                  (execution.status === 'completed'
                    ? '문서와 위키에 변경이 반영되었습니다.'
                    : execution.status === 'undone'
                      ? '문서 변경을 되돌렸습니다.'
                      : '문서 변경 처리가 종료되었습니다.')}
              </span>
              {execution.canRetry && (
                <button
                  disabled={action !== null}
                  onClick={() => void runExecutionAction('retry')}
                  className="font-semibold text-blue-600 hover:text-blue-700 disabled:opacity-50"
                >
                  {action === 'retry' ? '재시도 중…' : '동기화 재시도'}
                </button>
              )}
              {execution.canUndo && (
                <button
                  disabled={action !== null}
                  onClick={() => void runExecutionAction('undo')}
                  className="font-semibold text-red-600 hover:text-red-700 disabled:opacity-50"
                >
                  {action === 'undo' ? '되돌리는 중…' : 'Undo'}
                </button>
              )}
            </div>
            {actionError && <p className="mt-2 text-red-600">{actionError}</p>}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 승인으로 손댄 문서를 바로 열 수 있는 줄.
 *
 * 이게 없으면 "수정을 적용했습니다" 라는 문장만 남아, 사용자가 결과를 보려면 앱을 벗어나
 * 파일을 직접 찾아가야 한다. 여는 것과 폴더에서 보는 것을 나눠 둔 이유는 **백업** 때문이다 —
 * 되돌리려면 옆에 생긴 `.bak` 을 봐야 하는데, 파일을 여는 것만으로는 그게 보이지 않는다.
 */
function ChangedFileBar({ path }: { path: string }) {
  const [error, setError] = useState<string | null>(null);
  const name = path.split('/').pop() || path;

  async function run(action: 'openOriginal' | 'revealOriginal') {
    setError(null);
    try {
      await window.codegate.shell[action](path);
    } catch (err) {
      // 경로가 어긋났을 때 아무 일도 안 일어난 것처럼 보이면 안 된다.
      setError(err instanceof Error ? err.message : '문서를 열지 못했습니다.');
    }
  }

  return (
    <div className="mt-3.5 rounded-md border border-ink-100 bg-ink-50 px-3.5 py-3 text-xs">
      <div className="flex items-center gap-2">
        <span className="shrink-0 text-ink-400">대상 문서</span>
        <span className="min-w-0 flex-1 truncate font-mono text-2xs text-ink-600" title={path}>
          {name}
        </span>
        <button
          onClick={() => void run('openOriginal')}
          className="shrink-0 font-semibold text-blue-600 hover:text-blue-700"
        >
          열기
        </button>
        <button
          onClick={() => void run('revealOriginal')}
          className="shrink-0 font-semibold text-ink-500 hover:text-ink-700"
        >
          폴더에서 보기
        </button>
      </div>
      {error && <p className="mt-2 text-red-600">{error}</p>}
    </div>
  );
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 py-1.5" aria-label="생각하는 중">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 rounded-full bg-ink-300"
          style={{ animation: `fold-caret 1.2s ease-in-out ${i * 0.16}s infinite` }}
        />
      ))}
    </div>
  );
}
