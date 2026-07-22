import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';

/**
 * Folding 카드 — 흰 배경, 1px 헤어라인, 18px 라운드, shadow-xs.
 * 인터랙티브 카드만 hover 시 2px 떠오른다. 색 좌측 보더 금지.
 */
export function Card({
  interactive = false,
  selected = false,
  className,
  children,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  interactive?: boolean;
  selected?: boolean;
  children?: ReactNode;
}) {
  return (
    <div
      className={cn(
        'bg-white rounded-xl shadow-xs',
        selected
          ? 'border-[1.5px] border-blue-500'
          : 'border border-ink-100',
        interactive &&
          'cursor-pointer transition-[box-shadow,transform] duration-[--dur-fast] ease-[--ease-standard] hover:-translate-y-0.5 hover:shadow-lg',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}
