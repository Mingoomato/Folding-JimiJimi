import { describe, expect, it } from 'vitest';
import { splitPages } from '@renderer/lib/pages';

/**
 * `<!--- page 3 --->` 는 doc2md 의 장 구분 표시다.
 *
 * 하이픈이 셋이라 **표준 HTML 주석이 아니고**, 마크다운 파서가 걷어내 주지 않는다.
 * 실제로 react-markdown 은 이걸 `&lt;!--- page 3 ---&gt;` 로 이스케이프해 화면에 뱉었다.
 * "파서가 알아서 지워 주겠지"라고 넘겼다가 그대로 노출된 적이 있어 여기서 못 박는다.
 */
describe('splitPages', () => {
  it('페이지 표시를 본문에서 걷어내고 쪽 번호로 바꾼다', () => {
    const segments = splitPages('첫 장 내용\n<!--- page 2 --->\n둘째 장 내용');

    expect(segments).toHaveLength(2);
    expect(segments[0]).toEqual({ page: null, text: '첫 장 내용\n' });
    expect(segments[1].page).toBe(2);
    expect(segments[1].text).toContain('둘째 장 내용');
    // 어떤 조각에도 원본 표시가 남아선 안 된다.
    expect(segments.some((segment) => segment.text.includes('page 2'))).toBe(false);
  });

  it('하이픈 개수와 공백이 흔들려도 잡아낸다', () => {
    for (const marker of ['<!--- page 7 --->', '<!-- page 7 -->', '<!----  page 7  ---->']) {
      const segments = splitPages(`앞\n${marker}\n뒤`);
      expect(segments.at(-1)?.page, marker).toBe(7);
      expect(segments.some((s) => s.text.includes('page 7')), marker).toBe(false);
    }
  });

  it('표시가 없으면 통째로 한 조각이다 — 멀쩡한 본문을 쪼개지 않는다', () => {
    const segments = splitPages('# 제목\n\n본문입니다');
    expect(segments).toEqual([{ page: null, text: '# 제목\n\n본문입니다' }]);
  });

  it('여러 번 불러도 같은 결과다 (전역 정규식 상태 누수 방지)', () => {
    const source = '가\n<!--- page 2 --->\n나';
    expect(splitPages(source)).toEqual(splitPages(source));
  });

  it('본문 중간의 page 라는 낱말은 건드리지 않는다', () => {
    const segments = splitPages('this page is important');
    expect(segments).toEqual([{ page: null, text: 'this page is important' }]);
  });
});
