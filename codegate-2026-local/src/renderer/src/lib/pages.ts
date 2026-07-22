/**
 * doc2md 가 장 구분에 심는 표시 — `<!--- page 3 --->` 를 본문에서 떼어 낸다.
 *
 * 하이픈이 셋이라 **표준 HTML 주석이 아니다.** 그래서 마크다운 파서가 주석으로 걷어내지
 * 않고 글자 그대로 화면에 뱉는다(`&lt;!--- page 3 ---&gt;`). "파서가 알아서 지워 주겠지"로
 * 넘겼다가 그대로 노출된 적이 있어, 여기서 명시적으로 처리한다.
 *
 * 지우지 않고 쪽 번호로 살려 둔다 — "몇 쪽에서 온 내용인가"는 문서 도우미에서 쓸모가 있다.
 *
 * 렌더링과 섞지 않고 이 파일에 따로 둔 이유는 **테스트 때문**이다. JSX 안에 있으면
 * node 환경 vitest 가 불러올 수 없다.
 */
const PAGE_MARKER = /^[ \t]*<!-+[ \t]*page[ \t]+(\d+)[ \t]*-+>[ \t]*$/gim;

export interface PageSegment {
  /** 이 조각 앞에 붙일 쪽 번호. 첫 조각은 없을 수 있다. */
  page: number | null;
  text: string;
}

/** 페이지 표시를 기준으로 본문을 조각낸다. 표시가 없으면 통째로 한 조각. */
export function splitPages(source: string): PageSegment[] {
  const segments: PageSegment[] = [];
  let lastIndex = 0;
  let page: number | null = null;
  PAGE_MARKER.lastIndex = 0; // 전역 정규식은 호출 간 상태가 남는다
  for (let match = PAGE_MARKER.exec(source); match !== null; match = PAGE_MARKER.exec(source)) {
    const text = source.slice(lastIndex, match.index);
    if (text.trim() || segments.length === 0) segments.push({ page, text });
    page = Number(match[1]);
    lastIndex = match.index + match[0].length;
  }
  segments.push({ page, text: source.slice(lastIndex) });
  return segments.filter((segment, index) => segment.text.trim() || segment.page !== null || index === 0);
}
