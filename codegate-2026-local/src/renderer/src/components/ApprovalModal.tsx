import { FileWarning } from 'lucide-react';
import type { ApprovalEnvelope } from '@contracts';
import { Dialog, DialogContent, DialogFooter, DialogHeader } from '@/ui/Dialog';
import { Button } from '@/ui/Button';
import { DiffView } from './DiffView';

const TOOL_LABEL: Record<string, string> = {
  patch_document: '문서 수정',
  create_document: '문서 생성',
  create_from_template: '양식 복제',
};

/**
 * A3 승인 게이트 (스펙 v1.3 §3 승인 모달).
 *
 * 메인 프로세스는 응답이 올 때까지 도구 실행을 멈추고 기다린다.
 * 따라서 이 모달은 **모든 종료 경로에서 반드시 응답해야 한다** —
 * 바깥 클릭으로 닫히지 않게 막고, Esc는 명시적 "거부"로 처리한다.
 */
export function ApprovalModal({
  pending,
  onApprove,
  onReject,
}: {
  pending: ApprovalEnvelope | null;
  onApprove: () => void;
  onReject: () => void;
}) {
  if (!pending) return null;
  const { request } = pending;

  return (
    <Dialog open>
      <DialogContent
        // 실수로 닫아 에이전트를 멈춰 세우지 않도록 바깥 클릭은 무시한다.
        onInteractOutside={(e) => e.preventDefault()}
        // Esc = 거부. 응답 없이 사라지는 경로를 만들지 않는다.
        onEscapeKeyDown={(e) => {
          e.preventDefault();
          onReject();
        }}
      >
        <DialogHeader
          eyebrow="승인 필요"
          title={TOOL_LABEL[request.tool] ?? request.tool}
          description={
            <span className="flex items-center gap-1.5">
              <FileWarning size={14} className="shrink-0 text-amber-500" />
              승인하면 원본 문서가 바뀝니다. 수정 전 백업이 만들어집니다.
            </span>
          }
        />

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-auto px-7 py-5">
          <div>
            <div className="fold-eyebrow mb-1.5">대상</div>
            <div
              data-selectable
              className="break-all rounded-md bg-ink-50 px-3 py-2 font-mono text-xs text-ink-700"
            >
              {request.target}
            </div>
          </div>

          {request.rationale && (
            <div>
              <div className="fold-eyebrow mb-1.5">근거</div>
              <p data-selectable className="text-sm leading-relaxed text-ink-600">
                {request.rationale}
              </p>
            </div>
          )}

          {request.planHash && (
            <div>
              <div className="fold-eyebrow mb-1.5">plan_hash</div>
              <div
                data-selectable
                className="break-all rounded-md bg-ink-50 px-3 py-2 font-mono text-2xs text-ink-700"
              >
                {request.planHash}
              </div>
            </div>
          )}

          <DiffView diff={request.diff} />
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onReject}>
            거부
          </Button>
          <Button
            variant="secondary"
            onClick={() => window.codegate.shell.openOriginal(request.target)}
          >
            원본 보기
          </Button>
          <Button onClick={onApprove}>승인</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
