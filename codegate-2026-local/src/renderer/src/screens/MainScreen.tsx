import { useCallback, useState } from 'react';
import type { Session } from '@contracts';
import type { useWorkspace } from '@/state/useWorkspace';
import { useChat } from '@/state/useChat';
import { ChatList } from '@/components/ChatList';
import { ChatPane } from '@/components/ChatPane';
import { GraphPane } from '@/components/GraphPane';
import { TreePane } from '@/components/TreePane';
import { TitleBar } from '@/ui/TitleBar';
import { Badge } from '@/ui/Badge';
import { cn } from '@/lib/cn';

type Workspace = ReturnType<typeof useWorkspace>;
type Tab = 'chat' | 'graph';

/**
 * 메인 화면 (스펙 v1.3 §3).
 *
 *   ┌─ 타이틀바 (로고 · 탭 · 신호등 자리) ────────────┐
 *   ├─────────┬──────────────────┬──────────────────┤
 *   │ 채팅목록 │  채팅 | 그래프    │  트리 + 진행률    │
 *   └─────────┴──────────────────┴──────────────────┘
 *
 * 가운데 칸만 탭으로 바뀐다 — 좌우(채팅 목록·문서 트리)는 어느 탭에서도 그대로다.
 * 그래야 트리에서 문서를 고르고 그래프에서 확인하는 흐름이 끊기지 않는다.
 */
export function MainScreen({
  workspace,
  session,
  onOpenSettings,
}: {
  workspace: Workspace;
  session: Session;
  onOpenSettings: () => void;
}) {
  const chat = useChat();
  const [tab, setTab] = useState<Tab>('chat');
  const [focusedPath, setFocusedPath] = useState<string | null>(null);
  const webDemo = document.documentElement.dataset.runtime === 'web-demo';

  /** 트리에서 더블클릭 → 그래프 탭으로 옮기고 그 파일을 짚어 준다. */
  const openInGraph = useCallback((path: string) => {
    setFocusedPath(path);
    setTab('graph');
  }, []);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-ink-50">
      <TitleBar
        center={<Tabs value={tab} onChange={setTab} />}
        right={
          <Badge tone={session.provisioned ? 'brand' : 'danger'}>
            {webDemo ? '웹 데모' : session.provisioned ? '계정 연결됨' : '권한 확인 필요'}
          </Badge>
        }
      />

      <div className="grid min-h-0 flex-1 grid-cols-[var(--pane-chatlist)_1fr_var(--pane-tree)]">
        <ChatList
          conversations={chat.conversations}
          activeId={chat.activeId}
          onSelect={chat.setActiveId}
          onCreate={chat.createConversation}
          session={session}
          onOpenSettings={onOpenSettings}
        />

        {/*
         * 채팅은 **언제나 마운트된 채로 둔다** — 숨기기만 한다.
         * 언마운트하면 스트리밍 중인 답변과 승인 대기가 끊긴다.
         */}
        {/*
         * `grid-rows-[1fr]` — 암시적 행은 내용 높이만 차지해서 채팅 아래에 빈 공간이 생긴다.
         * `min-w-0` — 없으면 안의 SVG 가 넓어질 때 1fr 트랙이 함께 늘어나 **우측 문서 패널을
         * 화면 밖으로 밀어낸다**(1fr 은 최소 콘텐츠 폭을 존중한다).
         */}
        <div className="grid min-h-0 min-w-0 grid-rows-[1fr]">
          <div
            className={cn(
              'col-start-1 row-start-1 min-h-0 min-w-0',
              tab === 'chat' ? '' : 'hidden',
            )}
          >
            <ChatPane chat={chat} build={workspace.build} />
          </div>
          {tab === 'graph' && (
            <div className="col-start-1 row-start-1 min-h-0 min-w-0">
              <GraphPane focusPath={focusedPath} />
            </div>
          )}
        </div>

        <TreePane
          tree={workspace.tree}
          build={workspace.build}
          roots={workspace.roots}
          onOpenInGraph={openInGraph}
        />
      </div>
    </div>
  );
}

function Tabs({ value, onChange }: { value: Tab; onChange: (tab: Tab) => void }) {
  return (
    <div className="flex items-center gap-0.5 rounded-md bg-ink-100 p-0.5" role="tablist">
      {(
        [
          ['chat', '채팅'],
          ['graph', '그래프'],
        ] as const
      ).map(([key, label]) => (
        <button
          key={key}
          role="tab"
          aria-selected={value === key}
          onClick={() => onChange(key)}
          className={cn(
            'rounded-[5px] px-3 py-1 text-xs font-semibold transition-colors fold-focus',
            value === key ? 'bg-white text-ink-800 shadow-sm' : 'text-ink-500 hover:text-ink-700',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
