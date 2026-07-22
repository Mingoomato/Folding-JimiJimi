import { cn } from '@/lib/cn';

/**
 * Folding 브랜드 자산.
 *
 * 원본 PNG는 디자인 프로젝트에서 받아 `scripts/trim-brand-assets.mjs` 로
 * 여백을 잘라내고 흰 배경을 투명으로 바꿔 `public/brand/` 에 번들했다.
 * 원격 링크를 쓰지 않는다 — 앱은 오프라인에서도 동작해야 한다.
 */

const MARK_SRC = '/brand/folding-mark.png';
const LOGO_SRC = '/brand/folding-logo.png';

/** 폴더 마크만. 세로가 긴 비율(632×774)이라 높이를 기준으로 잡는다. */
export function Mark({ size = 26, className }: { size?: number; className?: string }) {
  return (
    <img
      src={MARK_SRC}
      alt=""
      aria-hidden="true"
      draggable={false}
      className={cn('shrink-0 select-none object-contain', className)}
      style={{ height: size, width: 'auto' }}
    />
  );
}

/**
 * 마크 + folding 워드마크 (1084×360).
 * 워드마크가 네이비라 **밝은 배경에서만** 쓴다. 어두운 면에는 MarkWithText 를 쓸 것.
 */
export function Logo({ height = 24, className }: { height?: number; className?: string }) {
  return (
    <img
      src={LOGO_SRC}
      alt="folding"
      draggable={false}
      className={cn('shrink-0 select-none object-contain', className)}
      style={{ height, width: 'auto' }}
    />
  );
}

/**
 * 어두운 면(로그인 레일)용.
 * 로고 PNG의 워드마크는 네이비여서 안 보이므로, 마크 + 흰 텍스트로 조판한다.
 * 조판 규칙은 디자인 시스템 그대로: 소문자·800·-0.04em.
 */
export function MarkWithText({
  markSize = 26,
  fontSize = 22,
  className,
}: {
  markSize?: number;
  fontSize?: number;
  className?: string;
}) {
  return (
    <span className={cn('inline-flex items-center gap-2.5', className)}>
      <Mark size={markSize} />
      <span
        className="font-extrabold leading-none text-white"
        style={{ fontSize, letterSpacing: '-0.04em' }}
      >
        folding
      </span>
    </span>
  );
}
