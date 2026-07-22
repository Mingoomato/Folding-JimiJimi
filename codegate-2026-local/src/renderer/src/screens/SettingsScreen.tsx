import { useState } from 'react';
import { FolderOpen, Plus, Trash2 } from 'lucide-react';
import type { Session } from '@contracts';
import type { useWorkspace } from '@/state/useWorkspace';
import { Dialog, DialogContent, DialogFooter, DialogHeader } from '@/ui/Dialog';
import { Button } from '@/ui/Button';
import { Badge } from '@/ui/Badge';

type Workspace = ReturnType<typeof useWorkspace>;

/**
 * 설정 — 계정·구독 상태와 등록 폴더 관리 (스펙 v2.4).
 *
 * 문서 분석 키 입력란은 없다. 토큰은 구독으로 우리가 내므로 사용자가 키를 넣을 일이 없다.
 * 키는 메인 프로세스의 ManagedLlmKeyStore 가 환경변수에서 공급한다.
 */
export function SettingsScreen({
  open,
  onClose,
  session,
  workspace,
  onLogout,
}: {
  open: boolean;
  onClose: () => void;
  session: Session;
  workspace: Workspace;
  onLogout: () => Promise<void>;
}) {
  const [folderError, setFolderError] = useState<string | null>(null);

  async function addFolder() {
    setFolderError(null);
    try {
      const path = await workspace.pickFolder();
      if (!path) return;
      await workspace.addRoot(path);
    } catch (error) {
      setFolderError(error instanceof Error ? error.message : '폴더를 등록하지 못했습니다.');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="w-[min(640px,calc(100vw-64px))]">
        <DialogHeader title="설정" />

        <div className="flex min-h-0 flex-1 flex-col gap-7 overflow-auto px-7 py-6">
          {/* ── 계정 · 구독 ── */}
          <section>
            <h3 className="fold-eyebrow mb-3">계정</h3>
            <div className="flex items-center gap-3 rounded-md border border-ink-100 px-4 py-3.5">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold text-ink-900">
                  {session.email ?? '로그인됨'}
                </div>
                <div className="mt-0.5 text-xs text-ink-400">
                  tenant {session.tenantId ?? '미확인'} · 쓰기 범위 {session.writeScope ?? 'none'}
                </div>
              </div>
              <Badge tone={session.provisioned ? 'success' : 'danger'}>
                {session.provisioned ? '권한 연결됨' : '권한 없음'}
              </Badge>
            </div>
            {!session.provisioned && (
              <p className="mt-2 text-xs text-red-600">
                계정 provisioning이 끝나기 전에는 문서 쓰기 승인을 실행하지 않습니다.
              </p>
            )}
          </section>

          {/* ── 등록 폴더 ── */}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="fold-eyebrow">등록 폴더</h3>
              <Button
                variant="ghost"
                size="sm"
                iconLeft={<Plus size={14} />}
                onClick={addFolder}
                disabled={workspace.roots.length > 0}
                title={
                  workspace.roots.length > 0
                    ? '기존 폴더를 해제한 뒤 새 폴더를 등록하세요.'
                    : undefined
                }
              >
                폴더 추가
              </Button>
            </div>
            <ul className="flex flex-col gap-2">
              {workspace.roots.map((r) => (
                <li
                  key={r.id}
                  className="flex items-center gap-3 rounded-md border border-ink-100 px-4 py-3"
                >
                  <FolderOpen size={16} className="shrink-0 text-ink-400" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-ink-800">{r.path}</div>
                    <div className="mt-0.5 font-mono text-2xs text-ink-400">
                      {r.includedCount}개 포함 · {r.excludedCount}개 제외
                    </div>
                  </div>
                  <button
                    onClick={() => {
                      setFolderError(null);
                      void workspace.removeRoot(r.id).catch((error: unknown) => {
                        setFolderError(
                          error instanceof Error ? error.message : '폴더를 해제하지 못했습니다.',
                        );
                      });
                    }}
                    aria-label="폴더 해제"
                    className="shrink-0 rounded-sm p-1.5 text-ink-300 transition-colors hover:bg-red-50 hover:text-red-500 fold-focus"
                  >
                    <Trash2 size={15} />
                  </button>
                </li>
              ))}
              {workspace.roots.length === 0 && (
                <li className="rounded-md border border-dashed border-ink-150 px-4 py-6 text-center text-xs text-ink-400">
                  등록된 폴더가 없습니다.
                </li>
              )}
            </ul>
            {workspace.roots.length > 0 && !folderError && (
              <p className="mt-2 text-xs text-ink-400">
                현재는 폴더 하나씩 동기화합니다. 다른 폴더를 쓰려면 기존 폴더를 먼저 해제하세요.
              </p>
            )}
            {folderError && (
              <p role="alert" className="mt-2 text-xs text-red-600">
                {folderError}
              </p>
            )}
          </section>
        </div>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={async () => {
              await onLogout();
              onClose();
            }}
          >
            로그아웃
          </Button>
          <Button variant="secondary" onClick={onClose}>
            닫기
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
