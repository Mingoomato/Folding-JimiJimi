/**
 * 표시용 포매터.
 * 백엔드는 날짜를 ISO 8601 로 준다 — 사용자에게 그대로 보여주지 않는다.
 */

/**
 * ISO 날짜 → `2026-12-31`. 파싱에 실패하면 원문을 그대로 돌려준다.
 *
 * **UTC 기준으로 읽는다.** 백엔드는 만료를 `2026-12-31T23:59:59Z` 처럼 그 날의 끝으로
 * 표현하는데, 이를 KST(+9)로 환산하면 2027-01-01 이 되어 하루 밀려 보인다.
 * 사용자가 기대하는 건 "12월 31일까지"이므로 시간대 변환 없이 날짜 부분만 쓴다.
 */
export function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** 상대 시각 — 채팅 목록용. */
export function formatRelative(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diffMin = Math.floor((Date.now() - d.getTime()) / 60000);
  if (diffMin < 1) return '방금';
  if (diffMin < 60) return `${diffMin}분 전`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}시간 전`;
  return d.toLocaleDateString('ko-KR', { month: 'numeric', day: 'numeric' });
}
