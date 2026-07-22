import * as RD from '@radix-ui/react-dialog';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

/**
 * Folding 모달.
 * 오버레이는 네이비 틴트(중성 검정 금지), 패널은 24px 라운드 + shadow-xl.
 * 등장은 짧게(200ms) — 바운스·긴 연출 없음.
 */
export function Dialog({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  return (
    <RD.Root open={open} onOpenChange={onOpenChange}>
      {children}
    </RD.Root>
  );
}

export function DialogContent({
  className,
  children,
  onEscapeKeyDown,
  onInteractOutside,
}: {
  className?: string;
  children: ReactNode;
  onEscapeKeyDown?: (e: KeyboardEvent) => void;
  onInteractOutside?: (e: Event) => void;
}) {
  return (
    <RD.Portal>
      <RD.Overlay
        className="fixed inset-0 z-50 bg-[rgba(13,22,48,.42)] backdrop-blur-[2px]"
        style={{ animation: 'fold-fade-in var(--dur-base) var(--ease-standard)' }}
      />
      <RD.Content
        onEscapeKeyDown={onEscapeKeyDown}
        onInteractOutside={onInteractOutside}
        className={cn(
          'fixed left-1/2 top-1/2 z-50 w-[min(760px,calc(100vw-64px))]',
          '-translate-x-1/2 -translate-y-1/2',
          'flex max-h-[calc(100vh-96px)] flex-col overflow-hidden',
          'rounded-2xl border border-ink-100 bg-white shadow-xl',
          className,
        )}
        style={{ animation: 'fold-rise var(--dur-base) var(--ease-out)' }}
      >
        {children}
      </RD.Content>
    </RD.Portal>
  );
}

export function DialogHeader({
  eyebrow,
  title,
  description,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 border-b border-ink-100 px-7 pb-5 pt-6">
      {eyebrow && <span className="fold-eyebrow">{eyebrow}</span>}
      <RD.Title className="text-h4 font-bold tracking-tight text-ink-950">{title}</RD.Title>
      {description && (
        <RD.Description asChild>
          <div className="text-sm text-ink-500">{description}</div>
        </RD.Description>
      )}
    </div>
  );
}

export function DialogFooter({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center justify-end gap-2.5 border-t border-ink-100 bg-ink-50 px-7 py-4">
      {children}
    </div>
  );
}
