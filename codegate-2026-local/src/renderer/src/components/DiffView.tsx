import { useMemo } from 'react';
import { cn } from '@/lib/cn';

type Line = { kind: 'add' | 'del' | 'meta' | 'hunk' | 'ctx'; text: string };

/** 통합 diff를 줄 단위로 분류한다. */
function parse(diff: string): Line[] {
  return diff.split('\n').map((text) => {
    if (text.startsWith('+++') || text.startsWith('---') || text.startsWith('diff '))
      return { kind: 'meta', text };
    if (text.startsWith('@@')) return { kind: 'hunk', text };
    if (text.startsWith('+')) return { kind: 'add', text };
    if (text.startsWith('-')) return { kind: 'del', text };
    return { kind: 'ctx', text };
  });
}

/**
 * 승인 모달의 diff 뷰.
 * 색은 의미로만 — 추가는 green, 삭제는 red, 나머지는 ink.
 */
export function DiffView({ diff, className }: { diff: string; className?: string }) {
  const lines = useMemo(() => parse(diff), [diff]);
  const added = lines.filter((l) => l.kind === 'add').length;
  const removed = lines.filter((l) => l.kind === 'del').length;

  return (
    <div className={cn('overflow-hidden rounded-md border border-ink-100', className)}>
      <div className="flex items-center gap-3 border-b border-ink-100 bg-ink-50 px-3.5 py-2">
        <span className="fold-eyebrow">변경 내용</span>
        <span className="ml-auto font-mono text-2xs">
          <span className="text-green-600">+{added}</span>
          <span className="mx-1 text-ink-300">·</span>
          <span className="text-red-600">−{removed}</span>
        </span>
      </div>
      <div data-selectable className="max-h-[320px] overflow-auto bg-white py-1.5">
        {lines.map((l, i) => (
          <div
            key={i}
            className={cn(
              'whitespace-pre-wrap break-words px-3.5 py-[1px] font-mono text-xs leading-relaxed',
              l.kind === 'add' && 'bg-green-50 text-green-600',
              l.kind === 'del' && 'bg-red-50 text-red-600',
              l.kind === 'hunk' && 'text-blue-600',
              l.kind === 'meta' && 'text-ink-400',
              l.kind === 'ctx' && 'text-ink-600',
            )}
          >
            {l.text || ' '}
          </div>
        ))}
      </div>
    </div>
  );
}
