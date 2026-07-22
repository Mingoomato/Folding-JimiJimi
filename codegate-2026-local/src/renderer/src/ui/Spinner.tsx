import { cn } from '@/lib/cn';

/** 로딩 전용 회전자. 디자인 시스템: 회전은 로딩에만 쓴다. */
export function Spinner({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <span
      role="status"
      aria-label="로딩 중"
      className={cn('inline-block rounded-full border-2 border-current/30', className)}
      style={{
        width: size,
        height: size,
        borderTopColor: 'currentColor',
        animation: 'fold-spin .7s linear infinite',
      }}
    />
  );
}
