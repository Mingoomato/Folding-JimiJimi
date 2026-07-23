import { Trash2 } from 'lucide-react';
import type { Conversation } from '@contracts';
import { Button } from '@/ui/Button';
import { Dialog, DialogContent, DialogFooter, DialogHeader } from '@/ui/Dialog';

export function DeleteConversationDialog({
  conversation,
  deleting,
  error,
  onCancel,
  onConfirm,
}: {
  conversation: Conversation | null;
  deleting: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open={conversation !== null}
      onOpenChange={(open) => {
        if (!open && !deleting) onCancel();
      }}
    >
      <DialogContent
        className="w-[min(480px,calc(100vw-40px))]"
        onEscapeKeyDown={(event) => {
          if (deleting) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (deleting) event.preventDefault();
        }}
      >
        <DialogHeader
          eyebrow="채팅 관리"
          title="채팅을 삭제할까요?"
          description={
            <span className="flex items-start gap-1.5">
              <Trash2 size={14} className="mt-0.5 shrink-0 text-red-500" />
              <span>
                “{conversation?.title}”의 대화 내용과 Agent 대화 컨텍스트가 삭제됩니다.
              </span>
            </span>
          }
        />

        <div className="px-7 py-5">
          <p className="text-sm leading-relaxed text-ink-600">
            생성하거나 수정한 문서와 문서 변경 감사 기록은 삭제되지 않습니다.
          </p>
          {error && (
            <div
              role="alert"
              className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
            >
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button autoFocus variant="ghost" disabled={deleting} onClick={onCancel}>
            취소
          </Button>
          <Button variant="danger" loading={deleting} onClick={onConfirm}>
            삭제
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
