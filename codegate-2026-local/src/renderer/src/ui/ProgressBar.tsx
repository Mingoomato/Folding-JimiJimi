import { cn } from '@/lib/cn';

/**
 * Folding 진행률 바.
 * 채움 전환은 --dur-slow (320ms) — 빌드 진행률이 튀지 않고 흐르게.
 */
export function ProgressBar({
  value,
  className,
  tone = 'brand',
  height = 6,
  active = false,
}: {
  /** 0–100 */
  value: number;
  className?: string;
  tone?: 'brand' | 'success' | 'danger';
  height?: number;
  /** 세부 진행률을 알 수 없는 현재 작업도 멈춘 것처럼 보이지 않게 한다. */
  active?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, value));
  const fill =
    tone === 'success'
      ? 'var(--green-500)'
      : tone === 'danger'
        ? 'var(--red-500)'
        : 'var(--gradient-brand)';

  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn('w-full overflow-hidden rounded-full bg-ink-100', className)}
      style={{ height }}
    >
      <div
        className={cn(
          'h-full rounded-full transition-[width] duration-[--dur-slow] ease-[--ease-standard]',
          active && 'animate-pulse',
        )}
        style={{ width: `${pct}%`, background: fill }}
      />
    </div>
  );
}
