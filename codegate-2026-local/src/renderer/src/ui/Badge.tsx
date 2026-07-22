import { cva, type VariantProps } from 'class-variance-authority';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

/** Folding 상태 뱃지. 색은 의미로만 — 장식으로 쓰지 않는다. */
const badge = cva(
  'inline-flex items-center gap-1.5 rounded-full font-semibold whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'bg-ink-100 text-ink-600',
        brand: 'bg-blue-50 text-blue-600',
        success: 'bg-green-50 text-green-600',
        warning: 'bg-amber-50 text-amber-600',
        danger: 'bg-red-50 text-red-600',
      },
      size: {
        sm: 'h-[20px] px-2 text-2xs',
        md: 'h-[24px] px-2.5 text-xs',
      },
    },
    defaultVariants: { tone: 'neutral', size: 'sm' },
  },
);

export function Badge({
  tone,
  size,
  className,
  children,
}: VariantProps<typeof badge> & { className?: string; children?: ReactNode }) {
  return <span className={cn(badge({ tone, size }), className)}>{children}</span>;
}
