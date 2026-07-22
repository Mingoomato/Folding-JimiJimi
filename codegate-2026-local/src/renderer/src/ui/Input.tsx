import { forwardRef, type InputHTMLAttributes, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

/** Folding 입력 — 10px 라운드, 헤어라인, 포커스 시 브랜드 링. */
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          'h-[42px] w-full rounded-md border border-ink-150 bg-white px-3.5',
          'text-[15px] text-ink-900 placeholder:text-ink-400',
          'transition-[border-color,box-shadow] duration-[--dur-fast] ease-[--ease-standard]',
          'hover:border-ink-200',
          'focus:outline-none focus:border-blue-500 focus:shadow-[var(--focus-ring)]',
          'disabled:bg-ink-50 disabled:text-ink-400 disabled:cursor-not-allowed',
          className,
        )}
        {...rest}
      />
    );
  },
);

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-semibold text-ink-700">{label}</span>
      {children}
      {error ? (
        <span className="text-xs text-red-600">{error}</span>
      ) : hint ? (
        <span className="text-xs text-ink-400">{hint}</span>
      ) : null}
    </label>
  );
}
