import type { CSSProperties, ReactNode } from 'react';
import { cn } from '@/lib/cn';

/**
 * 앱 상단 타이틀바.
 *
 * 창 전체 폭을 차지하고, 본문 UI는 **전부 이 아래에서** 시작한다.
 * macOS 는 titleBarStyle: 'hiddenInset' 이라 신호등이 창 안에 떠 있으므로
 * 왼쪽 78px 를 비워 둔다. 바 전체가 드래그 영역이고, 안의 버튼만 예외다.
 */
export function TitleBar({
  center,
  right,
  className,
}: {
  /** 화면 전환 탭 등 — 가운데 고정. 드래그 영역 위에 있으므로 클릭이 먹도록 예외 처리한다. */
  center?: ReactNode;
  right?: ReactNode;
  className?: string;
}) {
  return (
    <header
      data-drag-region
      className={cn(
        'relative flex h-[var(--spacing-topbar)] shrink-0 items-center gap-3',
        'border-b border-ink-100 bg-white pl-[78px] pr-4',
        className,
      )}
    >
      {/*
        로고는 타이틀바에 두지 않는다 — 신호등 **아래**(좌측 패널 상단)로 내렸다.
        여기 왼쪽은 신호등 자리를 비워 두는 여백(pl-78)으로만 쓴다.
      */}
      {center && (
        // 바 전체가 창 드래그 영역이라, 탭은 명시적으로 빼 주지 않으면 클릭이 안 먹는다.
        <div
          data-no-drag
          className="absolute left-1/2 -translate-x-1/2"
          style={{ WebkitAppRegion: 'no-drag' } as CSSProperties}
        >
          {center}
        </div>
      )}
      {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </header>
  );
}
