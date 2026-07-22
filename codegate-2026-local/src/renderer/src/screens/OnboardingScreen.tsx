import { useState } from 'react';
import { FolderOpen, FolderPlus, X } from 'lucide-react';
import type { ScanPreview, Session } from '@contracts';
import type { useWorkspace } from '@/state/useWorkspace';
import { Button } from '@/ui/Button';
import { Card } from '@/ui/Card';
import { Badge } from '@/ui/Badge';
import { TitleBar } from '@/ui/TitleBar';
import { Spinner } from '@/ui/Spinner';
import { ProgressBar } from '@/ui/ProgressBar';

type Workspace = ReturnType<typeof useWorkspace>;

/**
 * 온보딩 (스펙 v2.4).
 *   로그인 → 폴더 등록 → 스캔 미리보기 → 로컬 빌드
 *
 * 예전에는 폴더 등록 앞에 "Anthropic 키 설정" 단계가 있었다. 지금은 **토큰을 우리가 내고
 * 사용자는 구독으로 쓴다** — 사용자가 키를 발급받을 이유가 없으므로 그 단계를 없앴다.
 * 키는 메인 프로세스의 ManagedLlmKeyStore 가 공급한다.
 */
export function OnboardingScreen({
  workspace,
  session,
}: {
  workspace: Workspace;
  session: Session;
}) {
  return (
    <Shell session={session}>
      <FolderStep workspace={workspace} />
    </Shell>
  );
}

function Shell({ session, children }: { session: Session; children?: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col overflow-hidden bg-ink-50">
      <TitleBar
        right={
          <Badge tone={session.provisioned ? 'brand' : 'danger'}>
            {session.provisioned ? '계정 연결됨' : '권한 확인 필요'}
          </Badge>
        }
      />
      <div className="mx-auto flex min-h-0 w-full max-w-[680px] flex-1 flex-col justify-center overflow-auto px-8 pb-16">
        {children}
      </div>
    </div>
  );
}

/* ── 폴더 등록 (현재 sidecar 계약: 한 번에 하나) ───────────────────────── */

function FolderStep({ workspace }: { workspace: Workspace }) {
  const [staged, setStaged] = useState<ScanPreview | null>(null);
  const [scanning, setScanning] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const webDemo = document.documentElement.dataset.runtime === 'web-demo';

  /** 현재 지식 파이프라인이 실제로 동기화할 폴더 하나만 고른다. */
  async function pick() {
    setError(null);
    const path = await workspace.pickFolder();
    if (!path) return;
    setScanning(true);
    try {
      setStaged(await workspace.scanPreview(path));
    } catch (e) {
      setError(e instanceof Error ? e.message : '폴더를 읽지 못했습니다.');
    } finally {
      setScanning(false);
    }
  }

  function removeStaged() {
    setStaged(null);
  }

  async function confirm() {
    if (!staged) return;
    setSubmitting(true);
    setError(null);
    try {
      await workspace.addRoot(staged.root);
    } catch (e) {
      setSubmitting(false);
      setError(e instanceof Error ? e.message : '빌드를 시작하지 못했습니다.');
    }
  }

  const totalIncluded = staged?.included.length ?? 0;

  return (
    <>
      <span className="fold-eyebrow">폴더 등록</span>
      <h1 className="mt-3 text-h2 font-bold tracking-tight text-ink-950">
        어떤 폴더를 정리할까요?
      </h1>
      <p className="mt-2.5 text-base leading-relaxed text-ink-500">
        {webDemo ? (
          <>
            선택한 폴더의 지원 파일명을 브라우저에서 정리합니다.
            <br />
            폴더를 지원하지 않는 브라우저에서는 안전한 샘플 문서를 사용합니다.
          </>
        ) : (
          <>
            선택한 폴더 하나의 문서를 읽어 위키로 만듭니다.
            <br />
            다른 폴더로 바꾸려면 설정에서 기존 폴더를 해제하세요. 위키는 이 컴퓨터에만
            저장돼요.
          </>
        )}
      </p>

      {!staged ? (
        <Card
          interactive
          onClick={scanning ? undefined : pick}
          className="mt-8 flex flex-col items-center gap-3.5 border-dashed px-8 py-14 text-center"
        >
          {scanning ? (
            <>
              <Spinner size={26} className="text-blue-500" />
              <span className="text-sm text-ink-500">폴더를 훑어보는 중…</span>
            </>
          ) : (
            <>
              <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-blue-500">
                <FolderPlus size={26} />
              </span>
              <div>
                <div className="text-[15px] font-semibold text-ink-900">폴더 선택하기</div>
                <div className="mt-1 text-xs text-ink-400">
                  hwp · hwpx · docx · pdf · md · txt 를 읽습니다
                </div>
              </div>
            </>
          )}
        </Card>
      ) : (
        <Card className="mt-8 overflow-hidden">
          <ul className="flex flex-col">
            <li className="flex items-center gap-3 px-6 py-4">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-blue-50 text-blue-500">
                <FolderOpen size={19} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[15px] font-semibold text-ink-900">
                  {staged.root}
                </div>
                <div className="mt-0.5 font-mono text-xs text-ink-400">
                  {staged.included.length}개 포함 · {staged.excluded.length}개 제외
                </div>
              </div>
              {!submitting && (
                <button
                  onClick={removeStaged}
                  aria-label="폴더 제거"
                  className="shrink-0 rounded-sm p-1.5 text-ink-300 transition-colors hover:bg-red-50 hover:text-red-500 fold-focus"
                >
                  <X size={16} />
                </button>
              )}
            </li>
          </ul>
        </Card>
      )}

      {error && (
        <div
          role="alert"
          className="mt-4 rounded-md border border-red-100 bg-red-50 px-3.5 py-3 text-xs text-red-600"
        >
          {error}
        </div>
      )}

      {staged && (
        <div className="mt-6 flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <Button size="lg" onClick={confirm} loading={submitting}>
              {submitting
                ? workspace.build?.phase?.name === 'convert'
                  ? `문서 변환 중 ${workspace.build.phase.done}/${workspace.build.phase.total}`
                  : (workspace.build?.message ?? '문서를 준비하는 중…')
                : `문서 ${totalIncluded}개로 시작하기`}
            </Button>
            <span className="text-xs text-ink-400">
              {webDemo
                ? '웹 데모에서는 파일 내용이나 API 키를 서버로 전송하지 않습니다.'
                : '변환 ▸ 분석 ▸ 조립 순서로 이 컴퓨터에서 처리합니다.'}
            </span>
          </div>
          {submitting && (
            <div className="flex flex-col gap-1.5" aria-live="polite">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <ProgressBar value={workspace.build?.progress ?? 0} tone="brand" active />
                </div>
                <span className="w-10 shrink-0 text-right font-mono text-xs text-blue-600">
                  {Math.round(workspace.build?.progress ?? 0)}%
                </span>
              </div>
              <span className="truncate text-xs text-ink-400">
                {workspace.build?.message ?? '문서를 준비하는 중…'}
              </span>
            </div>
          )}
        </div>
      )}
    </>
  );
}
