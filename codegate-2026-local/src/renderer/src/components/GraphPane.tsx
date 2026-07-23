import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { WikiGraph, WikiGraphEdge, WikiGraphNode } from '@contracts';
import { cn } from '@/lib/cn';

/**
 * 지식 그래프 — 활성 위키 빌드의 문서(노드)와 링크(엣지)를 그린다.
 *
 * **외부 그래프 라이브러리를 쓰지 않는다.** 문서 수십~수백 개 규모에서는 자체 힘-기반
 * 시뮬레이션으로 충분하고, 의존성 하나가 늘면 Electron 번들과 보안 검토가 함께 늘어난다.
 *
 * 시뮬레이션은 **계속 돌지 않는다** — 에너지가 충분히 낮아지면 멈추고, 드래그 같은
 * 입력이 있을 때만 다시 깨어난다. 가만히 있는 화면이 CPU 를 먹으면 노트북 배터리가 준다.
 */

/** 문서 종류별 색. */
const TYPE_COLOR: Record<string, string> = {
  regulation: '#2E74B5',
  policy: '#2E74B5',
  contract: '#C00000',
  procedure: '#548235',
  manual: '#548235',
  guide: '#548235',
  specification: '#7030A0',
  report: '#BF8F00',
  meeting_note: '#BF8F00',
  general: '#7F7F7F',
};

const SELECTED_COLOR = '#FF6B00';

/* ---- 시뮬레이션 상수 ---- */
const REPULSION = 9000; // 노드끼리 밀어내는 세기
const SPRING_LENGTH = 120; // 링크의 자연 길이
const SPRING_K = 0.02; // 스프링 강성 — 클수록 빨리 딸려온다
const CENTER_PULL = 0.006; // 그래프를 화면 중앙으로 모으는 힘
const DAMPING = 0.86; // 속도 감쇠 — 없으면 영원히 진동한다
const MAX_SPEED = 18; // 한 프레임 최대 이동 (드래그 시 폭주 방지)
const SLEEP_ENERGY = 0.02; // 이보다 조용해지면 시뮬레이션을 멈춘다

interface Body {
  node: WikiGraphNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

export function GraphPane({
  focusPath,
}: {
  /**
   * 트리에서 더블클릭해 들어온 **파일 경로**. 트리는 doc_id 를 모르므로 경로로 받아
   * 여기서 manifest 의 `sourceUri` 와 대조해 노드를 찾는다.
   */
  focusPath: string | null;
}) {
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    window.codegate.wiki
      .graph()
      .then((g) => alive && setGraph(g))
      .catch((err: unknown) => {
        // 그래프를 못 읽었으면 빈 화면이 아니라 이유를 보여준다.
        if (alive) setError(err instanceof Error ? err.message : '지식 그래프를 불러오지 못했습니다.');
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) return <Centered tone="error">{error}</Centered>;
  if (!graph) return <Centered>지식 그래프를 불러오는 중…</Centered>;
  if (graph.nodes.length === 0) {
    return (
      <Centered>
        아직 위키 빌드가 없습니다.
        <br />
        폴더를 등록하고 빌드를 한 번 돌리면 문서 사이의 관계가 여기에 나타납니다.
      </Centered>
    );
  }

  return <GraphCanvas graph={graph} focusPath={focusPath} />;
}

function GraphCanvas({ graph, focusPath }: { graph: WikiGraph; focusPath: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 900, h: 600 });
  const [picked, setPicked] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);

  /** 그리는 좌표는 ref 에 둔다 — 프레임마다 setState 하면 리렌더가 시뮬레이션을 못 따라온다. */
  const bodies = useRef<Body[]>([]);
  const [, forceRender] = useState(0);
  const running = useRef(false);
  const dragRef = useRef<{ docId: string; x: number; y: number } | null>(null);

  /** 화면 변환 — 휠 확대/축소. 시뮬레이션 좌표는 건드리지 않고 보는 방식만 바꾼다. */
  const [view, setView] = useState({ scale: 1, tx: 0, ty: 0 });
  const viewRef = useRef(view);
  viewRef.current = view;

  // 보이는 영역을 시뮬레이션 상자로 쓴다 — 중앙 인력이 이 상자의 중심을 향한다.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const observer = new ResizeObserver(([entry]) => {
      const box = entry?.contentRect;
      if (box && box.width > 0 && box.height > 0) setSize({ w: box.width, h: box.height });
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  const edges = graph.edges;
  const index = useMemo(() => {
    const map = new Map<string, number>();
    graph.nodes.forEach((n, i) => map.set(n.docId, i));
    return map;
  }, [graph]);

  /** 시뮬레이션을 깨운다. 이미 돌고 있으면 아무 일도 하지 않는다. */
  const wake = useCallback(() => {
    if (running.current) return;
    running.current = true;

    const step = () => {
      if (!running.current) return;
      const energy = tick(bodies.current, edges, index, size, dragRef.current);
      forceRender((v) => v + 1);
      if (energy < SLEEP_ENERGY && !dragRef.current) {
        running.current = false; // 잠재운다 — 드래그하면 다시 깨어난다
        return;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }, [edges, index, size]);

  // 그래프나 화면 크기가 바뀌면 배치를 다시 잡고 깨운다.
  useEffect(() => {
    bodies.current = seed(graph.nodes, size);
    wake();
    return () => {
      running.current = false;
    };
  }, [graph, size, wake]);

  /* ---- 드래그 ---- */

  /**
   * 화면 좌표 → 시뮬레이션 좌표.
   * 확대/이동을 적용한 뒤이므로 **역변환을 해야** 끌 때 커서와 점이 어긋나지 않는다.
   */
  const toLocal = (e: { clientX: number; clientY: number }) => {
    const box = svgRef.current?.getBoundingClientRect();
    const { scale, tx, ty } = viewRef.current;
    return {
      x: (e.clientX - (box?.left ?? 0) - tx) / scale,
      y: (e.clientY - (box?.top ?? 0) - ty) / scale,
    };
  };

  /** 휠 확대/축소 — **커서 아래 지점을 고정**한다. 중심 기준으로 키우면 보던 곳을 놓친다. */
  const onWheel = (e: React.WheelEvent) => {
    const box = svgRef.current?.getBoundingClientRect();
    const px = e.clientX - (box?.left ?? 0);
    const py = e.clientY - (box?.top ?? 0);
    setView((v) => {
      const next = clampScale(v.scale * Math.exp(-e.deltaY * 0.0015));
      const k = next / v.scale;
      return { scale: next, tx: px - (px - v.tx) * k, ty: py - (py - v.ty) * k };
    });
  };

  const onPointerDown = (docId: string) => (e: React.PointerEvent) => {
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const p = toLocal(e);
    dragRef.current = { docId, x: p.x, y: p.y };
    setDragging(docId);
    setPicked(docId);
    wake();
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragRef.current) return;
    const p = toLocal(e);
    dragRef.current = { ...dragRef.current, x: p.x, y: p.y };
    wake();
  };

  const endDrag = () => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(null);
    wake(); // 놓은 뒤 스스로 자리를 잡도록 한 번 더 돌린다
  };

  /* ---- 트리에서 들어온 파일 ---- */

  const fromTree = useMemo(() => matchNode(graph.nodes, focusPath), [graph.nodes, focusPath]);
  useEffect(() => {
    if (fromTree.node) setPicked(fromTree.node.docId);
  }, [fromTree]);

  const selectedDocId = picked;
  const list = bodies.current;
  const byId = new Map(list.map((b) => [b.node.docId, b]));
  const selected = selectedDocId ? byId.get(selectedDocId) : undefined;
  const touches = (from: string, to: string) =>
    !selectedDocId || from === selectedDocId || to === selectedDocId;

  return (
    <div className="flex h-full min-h-0 flex-col bg-white">
      <header className="flex items-center gap-3 border-b border-ink-100 px-5 py-3">
        <span className="text-xs font-semibold text-ink-700">지식 그래프</span>
        <span className="text-2xs text-ink-400">
          문서 {graph.nodes.length} · 관계 {graph.edges.length}
        </span>
        {graph.nodes.length > 0 && graph.edges.length === 0 && (
          <span className="text-2xs text-amber-600">
            검증된 문서 간 관계가 아직 없습니다
          </span>
        )}
        {focusPath && fromTree.reason && (
          /*
           * 조용히 넘어가면 "왜 안 켜지지" 가 된다. 그리고 **못 찾은 것과 여럿인 것은 다르다** —
           * 전자는 빌드를 돌리면 되고, 후자는 어느 문서인지 사람이 정해야 한다.
           */
          <span className="truncate text-2xs text-amber-600">
            {fromTree.reason === 'missing'
              ? `«${basename(focusPath)}» 는 아직 이 빌드에 없습니다`
              : `«${basename(focusPath)}» 에 연결된 문서가 여럿이라 하나를 고르지 못했습니다`}
          </span>
        )}
        <span className="flex-1" />
        <span className="shrink-0 text-2xs text-ink-300">점을 끌어 옮기고, 휠로 확대·축소</span>
        <span className="shrink-0 font-mono text-2xs text-ink-400">
          {Math.round(view.scale * 100)}%
        </span>
        <button
          onClick={() => setView({ scale: 1, tx: 0, ty: 0 })}
          className="shrink-0 text-2xs font-semibold text-ink-500 hover:text-ink-700"
        >
          맞춤
        </button>
        {selectedDocId && (
          <button
            onClick={() => setPicked(null)}
            className="shrink-0 text-2xs font-semibold text-ink-500 hover:text-ink-700"
          >
            선택 해제
          </button>
        )}
      </header>

      <div ref={hostRef} className="min-h-0 flex-1 overflow-hidden">
        <svg
          ref={svgRef}
          width={size.w}
          height={size.h}
          className="touch-none select-none"
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerLeave={endDrag}
          onWheel={onWheel}
          onClick={(e) => e.target === svgRef.current && setPicked(null)}
        >
          <defs>
            <marker
              id="graph-arrow"
              viewBox="0 0 10 10"
              refX="16"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#C9CDD4" />
            </marker>
          </defs>

          <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
          {edges.map((edge, i) => {
            const a = byId.get(edge.from);
            const b = byId.get(edge.to);
            if (!a || !b) return null;
            const lit = touches(edge.from, edge.to);
            return (
              <line
                key={`${edge.from}-${edge.to}-${edge.relationType}-${i}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={lit ? '#9AA1AC' : '#E7E9EE'}
                strokeWidth={lit ? 1.4 : 1}
                markerEnd="url(#graph-arrow)"
              >
                <title>{`${a.node.title} → ${b.node.title} (${edge.relationType})`}</title>
              </line>
            );
          })}

          {list.map((body) => {
            const isSelected = body.node.docId === selectedDocId;
            const isDragging = body.node.docId === dragging;
            const color = isSelected
              ? SELECTED_COLOR
              : (TYPE_COLOR[body.node.docType] ?? TYPE_COLOR.general);
            return (
              <g
                key={body.node.docId}
                transform={`translate(${body.x} ${body.y})`}
                onPointerDown={onPointerDown(body.node.docId)}
                className={isDragging ? 'cursor-grabbing' : 'cursor-grab'}
              >
                {isSelected && <circle r={16} fill={SELECTED_COLOR} opacity={0.18} />}
                <circle
                  r={isSelected || isDragging ? 9 : 6}
                  fill={color}
                  stroke="#fff"
                  strokeWidth={2}
                  opacity={body.node.status === 'active' ? 1 : 0.45}
                />
                <text
                  y={isSelected || isDragging ? 26 : 20}
                  textAnchor="middle"
                  className={cn('pointer-events-none text-[10px]', isSelected && 'font-semibold')}
                  fill={isSelected ? SELECTED_COLOR : '#5B616E'}
                >
                  {truncate(body.node.title)}
                </text>
                <title>{`${body.node.title}\n${body.node.docId} · ${body.node.docType}`}</title>
              </g>
            );
          })}
          </g>
        </svg>
      </div>

      {selected && <NodeDetail node={selected.node} />}
    </div>
  );
}

/**
 * 한 프레임. 되돌려주는 값은 전체 운동에너지 — 호출자가 이걸 보고 잠재울지 정한다.
 *
 * 드래그 중인 노드는 **힘을 받지 않고 커서에 붙는다.** 그래야 손이 이끄는 대로 움직이고,
 * 이웃들은 스프링을 통해 뒤따라온다.
 */
function tick(
  bodies: Body[],
  edges: WikiGraphEdge[],
  index: Map<string, number>,
  size: { w: number; h: number },
  drag: { docId: string; x: number; y: number } | null,
): number {
  const cx = size.w / 2;
  const cy = size.h / 2;

  // 반발
  for (let i = 0; i < bodies.length; i += 1) {
    for (let j = i + 1; j < bodies.length; j += 1) {
      const a = bodies[i]!;
      const b = bodies[j]!;
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) {
        // 완전히 겹치면 밀어낼 방향이 없다 — 인덱스로 정해진 방향을 준다(무작위 금지).
        dx = ((i % 7) - 3) || 1;
        dy = ((j % 5) - 2) || 1;
        d2 = dx * dx + dy * dy;
      }
      const d = Math.sqrt(d2);
      const f = REPULSION / d2;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    }
  }

  // 스프링 — 링크로 이어진 쌍
  for (const edge of edges) {
    const i = index.get(edge.from);
    const j = index.get(edge.to);
    if (i === undefined || j === undefined) continue;
    const a = bodies[i]!;
    const b = bodies[j]!;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - SPRING_LENGTH) * SPRING_K;
    a.vx += (dx / d) * f;
    a.vy += (dy / d) * f;
    b.vx -= (dx / d) * f;
    b.vy -= (dy / d) * f;
  }

  // 중앙 인력 — 그래프가 화면 밖으로 흘러가지 않게
  for (const body of bodies) {
    body.vx += (cx - body.x) * CENTER_PULL;
    body.vy += (cy - body.y) * CENTER_PULL;
  }

  let energy = 0;
  for (const body of bodies) {
    if (drag && body.node.docId === drag.docId) {
      // 끌리는 노드는 커서가 위치를 정한다. 속도를 0 으로 둬야 놓았을 때 튀지 않는다.
      body.x = drag.x;
      body.y = drag.y;
      body.vx = 0;
      body.vy = 0;
      continue;
    }
    body.vx *= DAMPING;
    body.vy *= DAMPING;
    body.vx = clamp(body.vx, MAX_SPEED);
    body.vy = clamp(body.vy, MAX_SPEED);
    body.x += body.vx;
    body.y += body.vy;
    // 화면 밖으로 나가지 않게 여백을 두고 가둔다.
    body.x = Math.min(Math.max(body.x, 40), Math.max(size.w - 40, 60));
    body.y = Math.min(Math.max(body.y, 30), Math.max(size.h - 40, 60));
    energy += body.vx * body.vx + body.vy * body.vy;
  }
  return energy / Math.max(bodies.length, 1);
}

function clamp(v: number, max: number): number {
  return Math.max(-max, Math.min(max, v));
}

/** 확대 배율 한계 — 너무 줄이면 점이 사라지고, 너무 키우면 길을 잃는다. */
function clampScale(v: number): number {
  return Math.max(0.3, Math.min(4, v));
}

/**
 * 초기 배치 — 화면 중앙을 둘러싼 원 위에 **결정적으로** 흩뿌린다.
 * `Math.random()` 을 쓰면 열 때마다 다른 그림이 나와 "아까 그 문서"를 못 찾는다.
 */
function seed(nodes: WikiGraphNode[], size: { w: number; h: number }): Body[] {
  const cx = size.w / 2;
  const cy = size.h / 2;
  const radius = Math.min(size.w, size.h) * 0.3 || 160;
  return nodes.map((node, i) => {
    const angle = (i / nodes.length) * Math.PI * 2 + ((hash(node.docId) % 100) / 100) * 0.5;
    const r = radius * (0.6 + ((hash(node.docId + '#r') % 100) / 100) * 0.4);
    return { node, x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r, vx: 0, vy: 0 };
  });
}

function NodeDetail({ node }: { node: WikiGraphNode }) {
  return (
    <footer className="border-t border-ink-100 bg-ink-50 px-5 py-3">
      <div className="flex items-center gap-2">
        <span className="truncate text-xs font-semibold text-ink-800">{node.title}</span>
        <span className="shrink-0 font-mono text-2xs text-ink-400">
          {node.docId} · {node.docType} · {node.status}
        </span>
        <span className="flex-1" />
        {node.sourceUri && (
          <button
            onClick={() => void window.codegate.shell.openOriginal(node.sourceUri!)}
            className="shrink-0 text-2xs font-semibold text-blue-600 hover:text-blue-700"
          >
            원본 열기
          </button>
        )}
      </div>
      {node.summary && <p className="mt-1.5 line-clamp-2 text-2xs text-ink-500">{node.summary}</p>}
    </footer>
  );
}

function Centered({ children, tone }: { children: React.ReactNode; tone?: 'error' }) {
  return (
    <div className="flex h-full items-center justify-center bg-white px-8 text-center">
      <p className={cn('text-sm leading-relaxed', tone === 'error' ? 'text-red-600' : 'text-ink-400')}>
        {children}
      </p>
    </div>
  );
}

/**
 * 트리의 파일 경로로 그래프 노드를 찾는다.
 *
 * 두 값의 모양이 다르다 — 트리는 `<rootId>/<상대경로>`, manifest 는 `source://<...>` 이거나
 * 절대경로다. 그래서 **뒤에서부터 겹치는지**로 맞춘다. 그래도 못 찾으면 파일명으로 한 번 더
 * 본다. 어느 쪽이든 **후보가 여럿이면 찍지 않는다** — 엉뚱한 노드를 켜 놓고 맞다고 하는
 * 것보다, 못 찾았다고 말하는 편이 낫다.
 */
function matchNode(
  nodes: WikiGraphNode[],
  path: string | null,
): { node?: WikiGraphNode; reason?: 'missing' | 'ambiguous' } {
  if (!path) return {};
  const tail = path.replace(/^source:\/\//, '');

  const exact = nodes.filter((n) => {
    const uri = n.sourceUri?.replace(/^source:\/\//, '');
    if (!uri) return false;
    return uri === tail || uri.endsWith(`/${tail}`) || tail.endsWith(`/${uri}`);
  });
  if (exact.length === 1) return { node: exact[0] };
  if (exact.length > 1) return { reason: 'ambiguous' };

  const name = basename(tail);
  const byName = nodes.filter((n) => (n.sourceFilename ?? basename(n.sourceUri ?? '')) === name);
  if (byName.length === 1) return { node: byName[0] };
  return { reason: byName.length > 1 ? 'ambiguous' : 'missing' };
}

function basename(path: string): string {
  return path.split('/').pop() || path;
}

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h);
}

function truncate(text: string, max = 14): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
