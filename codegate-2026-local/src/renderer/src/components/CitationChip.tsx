import type { Citation } from '@contracts';
import { formatCitation } from '@contracts';

/**
 * 인용 칩 — 클릭하면 원본을 연다 (스펙 v1.3 §1 L1).
 *
 * 화면에는 **파일명 + 섹션**을 보여준다. `K7M2QP` 같은 DOC-ID 는 시스템 식별자라
 * 사용자가 어느 문서인지 알아볼 수 없다. 정확한 인용 표기(`【DOC-ID rev.N §섹션】`)는
 * 계약의 `formatCitation` 그대로 **툴팁에** 남겨 둔다 — 근거를 추적할 때는 그게 필요하다.
 */
export function CitationChip({ citation }: { citation: Citation }) {
  const openable = Boolean(citation.sourcePath);
  const label = citation.sourcePath
    ? `${basename(citation.sourcePath)} §${citation.section}`
    : formatCitation(citation);

  return (
    <button
      disabled={!openable}
      onClick={() => citation.sourcePath && window.codegate.shell.openOriginal(citation.sourcePath)}
      title={
        openable
          ? `${formatCitation(citation)}\n원본 열기 — ${citation.sourcePath}\ngraph ${citation.graphVersion ?? '-'}\nchunk ${citation.chunkId ?? '-'}`
          : `${formatCitation(citation)}\n원본 경로를 알 수 없습니다`
      }
      className={
        'inline-flex items-center rounded-full bg-blue-50 px-2.5 py-1 font-mono text-2xs text-blue-600 ' +
        'transition-colors duration-[--dur-fast] ease-[--ease-standard] fold-focus ' +
        (openable
          ? 'cursor-pointer hover:bg-blue-100 active:bg-blue-200'
          : 'cursor-default opacity-70')
      }
    >
      {label}
    </button>
  );
}

function basename(path: string): string {
  return path.split('/').pop() || path;
}
