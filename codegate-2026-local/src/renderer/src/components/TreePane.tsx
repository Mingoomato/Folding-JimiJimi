import { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Check,
  ChevronRight,
  Circle,
  Folder,
  FolderOpen,
  Loader2,
} from 'lucide-react';
import type { BuildPhase, BuildState, FileNode, FileStatus, Root } from '@contracts';
import { BUILD_PHASE_LABEL } from '@contracts';
import { cn } from '@/lib/cn';
import { ProgressBar } from '@/ui/ProgressBar';

/** 우측 패널 — 디렉터리 트리(파일별 상태) + 전체 진행률 (스펙 v1.3 §1 L1). */
export function TreePane({
  tree,
  build,
  roots,
  onOpenInGraph,
}: {
  tree: FileNode[];
  build: BuildState | null;
  roots: Root[];
  /** 파일 더블클릭 → 그래프 탭에서 그 문서를 짚어 준다 (경로로 넘긴다 — 트리는 doc_id 를 모른다) */
  onOpenInGraph?: (path: string) => void;
}) {
  const counts = useMemo(() => countByStatus(tree), [tree]);
  const [menu, setMenu] = useState<{ path: string; x: number; y: number } | null>(null);

  return (
    <aside className="flex min-h-0 flex-col border-l border-ink-100 bg-white">
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-ink-100 px-5">
        <span className="fold-eyebrow">문서</span>
        <span className="font-mono text-2xs text-ink-400">
          {counts.done}/{counts.total}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 pb-3">
        {tree.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs leading-relaxed text-ink-400">
            아직 정리된 문서가 없습니다.
            <br />
            빌드가 끝나면 여기에 나타나요.
          </p>
        ) : (
          <ul className="flex flex-col">
            {tree.map((n) => (
              <TreeNode
                key={n.path}
                node={n}
                depth={0}
                onOpenInGraph={onOpenInGraph}
                onContextMenu={(path, x, y) => setMenu({ path, x, y })}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="shrink-0 border-t border-ink-100 px-5 py-4">
        <BuildSummary build={build} roots={roots} counts={counts} />
      </div>

      {menu && <ContextMenu {...menu} onClose={() => setMenu(null)} />}
    </aside>
  );
}

/**
 * 파일 우클릭 메뉴 — 열기 · 파인더에서 보기.
 *
 * 둘을 나눈 이유는 **백업** 때문이다. 문서를 고치면 옆에 `.bak` 이 생기는데,
 * 파일을 여는 것만으로는 그게 보이지 않는다. 되돌리려면 폴더를 봐야 한다.
 */
function ContextMenu({
  path,
  x,
  y,
  onClose,
}: {
  path: string;
  x: number;
  y: number;
  onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);

  // 바깥을 누르거나 Esc 로 닫는다 — 메뉴가 남아 화면을 가리면 안 된다.
  useEffect(() => {
    const close = () => onClose();
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  async function run(action: 'openOriginal' | 'revealOriginal') {
    try {
      await window.codegate.shell[action](path);
      onClose();
    } catch (err) {
      // 실패를 삼키고 메뉴만 닫으면 사용자는 뭘 눌렀는지 모른 채 아무 일도 안 일어난다.
      setError(err instanceof Error ? err.message : '문서를 열지 못했습니다.');
    }
  }

  return (
    <div
      // 메뉴 안의 클릭이 바깥 닫기 리스너까지 가지 않게 막는다.
      onMouseDown={(e) => e.stopPropagation()}
      style={{ left: x, top: y }}
      className="fixed z-50 min-w-[168px] overflow-hidden rounded-md border border-ink-100 bg-white py-1 shadow-lg"
      role="menu"
    >
      <MenuItem onClick={() => void run('openOriginal')}>열기</MenuItem>
      <MenuItem onClick={() => void run('revealOriginal')}>파인더에서 열기</MenuItem>
      {error && <p className="mt-1 border-t border-ink-100 px-3 py-2 text-2xs text-red-600">{error}</p>}
    </div>
  );
}

function MenuItem({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      role="menuitem"
      onClick={onClick}
      className="block w-full px-3 py-1.5 text-left text-xs text-ink-700 hover:bg-ink-50"
    >
      {children}
    </button>
  );
}

function TreeNode({
  node,
  depth,
  onOpenInGraph,
  onContextMenu,
}: {
  node: FileNode;
  depth: number;
  onOpenInGraph?: (path: string) => void;
  onContextMenu: (path: string, x: number, y: number) => void;
}) {
  const [open, setOpen] = useState(depth < 1);

  if (node.isDir) {
    return (
      <li>
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex w-full items-center gap-1.5 rounded-sm py-1.5 pr-2 text-left hover:bg-ink-50 fold-focus"
          style={{ paddingLeft: 8 + depth * 12 }}
        >
          <ChevronRight
            size={13}
            className={cn(
              'shrink-0 text-ink-300 transition-transform duration-[--dur-fast]',
              open && 'rotate-90',
            )}
          />
          {open ? (
            <FolderOpen size={14} className="shrink-0 text-ink-400" />
          ) : (
            <Folder size={14} className="shrink-0 text-ink-400" />
          )}
          <span className="truncate text-xs font-medium text-ink-700">{node.name}</span>
        </button>
        {open && node.children && node.children.length > 0 && (
          <ul className="flex flex-col">
            {node.children.map((c) => (
              <TreeNode
                key={c.path}
                node={c}
                depth={depth + 1}
                onOpenInGraph={onOpenInGraph}
                onContextMenu={onContextMenu}
              />
            ))}
          </ul>
        )}
      </li>
    );
  }

  return (
    <li>
      <button
        /*
         * 단일 클릭으로 파일을 열지 않는다.
         *
         * 예전에는 클릭 = 원본 열기였다. 여기에 더블클릭을 붙이면 **첫 클릭이 먼저 파일을
         * 열어버려** 외부 앱이 뜨고 포커스를 가져간다 — 더블클릭이 사실상 불가능해진다.
         * 그래서 여는 동작은 우클릭 메뉴로 옮겼다.
         */
        onDoubleClick={() => onOpenInGraph?.(node.path)}
        onContextMenu={(e) => {
          e.preventDefault();
          onContextMenu(node.path, e.clientX, e.clientY);
        }}
        title={`더블클릭: 그래프에서 보기 · 우클릭: 열기 메뉴\n${node.path}`}
        className="flex w-full items-center gap-2 rounded-sm py-1.5 pr-2 text-left hover:bg-ink-50 fold-focus"
        style={{ paddingLeft: 8 + depth * 12 + 19 }}
      >
        <StatusIcon status={node.status} />
        <span
          className={cn(
            'truncate text-xs',
            node.status === 'done' ? 'text-ink-600' : 'text-ink-400',
          )}
        >
          {node.name}
        </span>
      </button>
    </li>
  );
}

const STATUS_LABEL: Record<FileStatus, string> = {
  pending: '대기',
  converting: '변환 중',
  done: '완료',
  error: '실패',
};

function StatusIcon({ status }: { status: FileStatus }) {
  const common = 'shrink-0';
  switch (status) {
    case 'done':
      return <Check size={13} className={cn(common, 'text-green-500')} aria-label={STATUS_LABEL.done} />;
    case 'converting':
      return (
        <Loader2
          size={13}
          className={cn(common, 'text-blue-500')}
          style={{ animation: 'fold-spin 1s linear infinite' }}
          aria-label={STATUS_LABEL.converting}
        />
      );
    case 'error':
      return (
        <AlertCircle size={13} className={cn(common, 'text-red-500')} aria-label={STATUS_LABEL.error} />
      );
    default:
      return (
        <Circle size={13} className={cn(common, 'text-ink-300')} aria-label={STATUS_LABEL.pending} />
      );
  }
}

/** 파이프라인 3단계 표시 — 지금 어느 단계인지 한눈에 (스펙 v1.4 §3). */
function PhaseTrack({ current }: { current: BuildPhase }) {
  const order: BuildPhase[] = ['convert', 'enrich', 'assemble'];
  const idx = order.indexOf(current);
  return (
    <div className="flex items-center gap-1">
      {order.map((p, i) => (
        <span
          key={p}
          className={cn(
            'text-2xs font-semibold transition-colors duration-[--dur-base]',
            i < idx && 'text-ink-300',
            i === idx && 'text-blue-600',
            i > idx && 'text-ink-300',
          )}
        >
          {BUILD_PHASE_LABEL[p]}
          {i < order.length - 1 && <span className="mx-1 text-ink-200">▸</span>}
        </span>
      ))}
    </div>
  );
}

function BuildSummary({
  build,
  roots,
  counts,
}: {
  build: BuildState | null;
  roots: Root[];
  counts: { total: number; done: number };
}) {
  const failed = build?.status === 'failed';
  const running = build && build.status !== 'idle' && build.status !== 'done' && !failed;
  const deferred = build?.deferred ?? [];

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs font-semibold text-ink-700">
          {failed ? '빌드 실패' : running ? '정리 중' : '최신 상태'}
        </span>
        {/*
          수동 빌드 — 실패했을 때 되돌릴 길이 있어야 한다. 예전에는 실패하면 화면에서
          다시 시도할 방법이 없어 앱을 껐다 켜야 했다. 진행 중에는 중복 실행을 막는다.
        */}
        {!running && (
          <button
            onClick={() => void window.codegate.build.trigger()}
            className="shrink-0 text-2xs font-semibold text-blue-600 hover:text-blue-700"
          >
            {failed ? '다시 빌드' : '지금 빌드'}
          </button>
        )}
        {running && (
          <span className="shrink-0 font-mono text-xs text-blue-600">
            {Math.round(build.progress)}%
          </span>
        )}
      </div>

      {running && build.phase && <PhaseTrack current={build.phase.name} />}

      {(running || failed) && (
        <ProgressBar
          value={failed ? 100 : (build?.progress ?? 0)}
          tone={failed ? 'danger' : 'brand'}
          active={Boolean(running)}
        />
      )}

      <p className={cn('text-2xs leading-relaxed', failed ? 'text-red-600' : 'text-ink-400')}>
        {failed
          ? // 실패해도 이전 빌드는 그대로 유지된다 (스펙 v1.4 §5)
            (build?.error ?? '빌드에 실패했습니다. 이전 위키를 그대로 사용합니다.')
          : running
            ? `${build.message || '문서를 정리하는 중…'} · 현재 단계는 안전하게 중단할 수 없어 완료될 때까지 기다려 주세요.`
            : `${roots.length}개 폴더 · 문서 ${counts.total}개`}
      </p>

      {/* enrichment 부분 실패는 빌드를 죽이지 않고 해당 문서만 보류한다 (스펙 v1.4 §5) */}
      {deferred.length > 0 && (
        <p className="rounded-sm bg-amber-50 px-2 py-1.5 text-2xs leading-relaxed text-amber-600">
          {deferred.length}개 문서는 분석에 실패해 보류했습니다. 다음 빌드에서 다시 시도합니다.
        </p>
      )}
    </div>
  );
}

function countByStatus(nodes: FileNode[]): { total: number; done: number } {
  let total = 0;
  let done = 0;
  const walk = (list: FileNode[]) => {
    for (const n of list) {
      if (n.isDir) {
        if (n.children) walk(n.children);
      } else {
        total += 1;
        if (n.status === 'done') done += 1;
      }
    }
  };
  walk(nodes);
  return { total, done };
}
