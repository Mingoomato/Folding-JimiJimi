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
  const preview = request.documentPreview;

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
              {request.tool === 'create_document'
                ? '승인하면 선택한 경로에 새 파일을 만들고 LLMWIKI를 동기화합니다.'
                : '승인하면 원본 문서가 바뀝니다. 수정 전 백업이 만들어집니다.'}
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

          {preview && (
            <>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <Metadata label="형식" value={preview.format.toUpperCase()} />
                <Metadata label="Capability" value={preview.capabilityId} />
                <Metadata label="Writer" value={preview.writerFingerprint ?? '확인 불가'} />
                <Metadata label="Renderer" value={preview.rendererFingerprint ?? '확인 불가'} />
                {preview.sourceSha256 && (
                  <Metadata label="Source SHA-256" value={preview.sourceSha256} />
                )}
                {preview.proposedSha256 && (
                  <Metadata label="Artifact SHA-256" value={preview.proposedSha256} />
                )}
              </div>

              {preview.warnings.length > 0 && (
                <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  {preview.warnings.map((warning) => (
                    <div key={warning}>{warning}</div>
                  ))}
                </div>
              )}

              <div>
                <div className="fold-eyebrow mb-1.5">구조 변경</div>
                <div className="flex flex-col gap-2">
                  {preview.structuralDiff.map((item) => (
                    <div
                      key={`${item.operationIndex}:${item.operationType}`}
                      className="rounded-md border border-ink-100 bg-white px-3 py-2 text-xs"
                    >
                      <div className="mb-1 font-mono text-ink-700">{item.operationType}</div>
                      <div className="grid grid-cols-2 gap-2">
                        <ValueBlock label="변경 전" value={item.before} />
                        <ValueBlock label="변경 후" value={item.after} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {preview.images.length > 0 && (
                <div>
                  <div className="fold-eyebrow mb-1.5">렌더 비교</div>
                  <div className="flex flex-col gap-3">
                    {preview.images.map((pair) => (
                      <div key={pair.label} className="rounded-md border border-ink-100 p-2">
                        <div className="mb-2 text-xs font-medium text-ink-700">{pair.label}</div>
                        {pair.summaryOnly ? (
                          <div className="text-xs text-ink-500">요약만 제공되는 변경입니다.</div>
                        ) : (
                          <div className="grid grid-cols-2 gap-2">
                            <PreviewImage label="변경 전" dataUrl={pair.beforeDataUrl} />
                            <PreviewImage label="변경 후" dataUrl={pair.afterDataUrl} />
                          </div>
                        )}
                      </div>
                    ))}
                    {preview.truncatedCount > 0 && (
                      <div className="text-xs text-ink-500">
                        추가 변경 {preview.truncatedCount}개는 렌더 한도 때문에 구조 diff로만 표시됩니다.
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}

          {!preview && <DiffView diff={request.diff} />}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onReject}>
            거부
          </Button>
          {request.tool !== 'create_document' && (
            <Button
              variant="secondary"
              onClick={() => window.codegate.shell.openOriginal(request.target)}
            >
              원본 보기
            </Button>
          )}
          <Button onClick={onApprove}>승인</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Metadata({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md bg-ink-50 px-3 py-2">
      <div className="fold-eyebrow mb-1">{label}</div>
      <div data-selectable className="break-all font-mono text-2xs text-ink-700">
        {value}
      </div>
    </div>
  );
}

function ValueBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0 rounded bg-ink-50 px-2 py-1.5">
      <div className="mb-1 text-2xs text-ink-400">{label}</div>
      <pre data-selectable className="whitespace-pre-wrap break-words font-mono text-2xs text-ink-700">
        {formatValue(value)}
      </pre>
    </div>
  );
}

function PreviewImage({ label, dataUrl }: { label: string; dataUrl?: string }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-2xs text-ink-400">{label}</div>
      {dataUrl ? (
        <img src={dataUrl} alt={`${label} 문서 미리보기`} className="w-full rounded border" />
      ) : (
        <div className="flex min-h-24 items-center justify-center rounded bg-ink-50 text-xs text-ink-400">
          없음
        </div>
      )}
    </div>
  );
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined) return '없음';
  return JSON.stringify(value, null, 2) ?? String(value);
}
