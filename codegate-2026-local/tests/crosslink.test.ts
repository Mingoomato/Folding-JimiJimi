import { describe, expect, it } from 'vitest';
import {
  crossLinkCorpus,
  documentCodes,
  linkBody,
  uniqueCodeIndex,
  type CrossLinkDoc,
} from '@main/sidecar/crosslink';

/**
 * 텍스트 참조 → 마크다운 링크.
 *
 * LLMWIKI 빌더는 `[제목](DOC-xxxx.md)` 형태의 링크에서만 관계를 뽑는다. 변환된 office
 * 문서에는 그런 링크가 없고 문서 번호를 글로 적을 뿐이라, 상호 참조가 140군데 있어도
 * 간선이 0이었다. 여기서 그 간극을 메운다.
 *
 * 가장 중요한 것은 **없는 관계를 만들지 않는 것**이다. 잘못 이은 간선은 없는 간선보다
 * 나쁘다 — 사용자가 그래프를 근거로 판단하기 때문이다.
 */
describe('documentCodes', () => {
  it('문서 번호처럼 생긴 것만 뽑는다', () => {
    expect(documentCodes('GBX-ONB-001_고객온보딩_개선계획서.docx')).toEqual(['GBX-ONB-001']);
    expect(documentCodes('DOC-2026-07 회의록')).toEqual(['DOC-2026-07']);
  });

  it('날짜·인코딩 이름 같은 것은 번호로 보지 않는다', () => {
    // 숫자로 시작하는 날짜는 문서 번호가 아니다.
    expect(documentCodes('2026-07-22 주간보고.pdf')).toEqual([]);
    // 너무 짧거나 숫자가 없으면 문서 번호로 보기 어렵다.
    expect(documentCodes('UTF-8 인코딩')).toEqual([]);
    expect(documentCodes('A-B')).toEqual([]);
  });
});

describe('uniqueCodeIndex', () => {
  const docs = (): CrossLinkDoc[] => [
    { id: 'DOC-1', label: 'GBX-ONB-001_계획서.docx', body: '' },
    { id: 'DOC-2', label: 'GBX-ONB-002_회의록.docx', body: '' },
  ];

  it('번호마다 주인 문서를 찾는다', () => {
    expect(uniqueCodeIndex(docs())).toEqual(
      new Map([
        ['GBX-ONB-001', 'DOC-1'],
        ['GBX-ONB-002', 'DOC-2'],
      ]),
    );
  });

  it('같은 번호를 두 문서가 주장하면 아무 데도 잇지 않는다', () => {
    const ambiguous: CrossLinkDoc[] = [
      { id: 'DOC-1', label: 'GBX-ONB-001_원본.docx', body: '' },
      { id: 'DOC-2', label: 'GBX-ONB-001_사본.docx', body: '' },
    ];
    // 어느 쪽인지 모르면 찍지 않는다.
    expect(uniqueCodeIndex(ambiguous).size).toBe(0);
  });
});

describe('linkBody', () => {
  const index = new Map([
    ['GBX-ONB-001', 'DOC-1'],
    ['GBX-ONB-002', 'DOC-2'],
  ]);

  it('다른 문서를 가리키는 번호를 링크로 감싼다', () => {
    expect(linkBody('GBX-ONB-001 개선 계획을 따른다', index, 'DOC-2')).toBe(
      '[GBX-ONB-001](DOC-1.md) 개선 계획을 따른다',
    );
  });

  it('자기 자신은 연결하지 않는다 — 자기를 가리키는 간선은 의미가 없다', () => {
    expect(linkBody('본 문서 GBX-ONB-001 은', index, 'DOC-1')).toBe('본 문서 GBX-ONB-001 은');
  });

  it('모르는 번호는 그대로 둔다', () => {
    expect(linkBody('GBX-ONB-999 참조', index, 'DOC-1')).toBe('GBX-ONB-999 참조');
  });

  it('이미 링크인 곳을 두 번 감싸지 않는다', () => {
    const already = '[GBX-ONB-001](DOC-1.md) 과 GBX-ONB-002';
    expect(linkBody(already, index, 'DOC-3')).toBe(
      '[GBX-ONB-001](DOC-1.md) 과 [GBX-ONB-002](DOC-2.md)',
    );
  });

  it('표 안의 참조도 연결한다 — 변환 문서는 대부분 표다', () => {
    expect(linkBody('| 근거 | GBX-ONB-001 |', index, 'DOC-2')).toBe(
      '| 근거 | [GBX-ONB-001](DOC-1.md) |',
    );
  });
});

describe('crossLinkCorpus', () => {
  it('바뀐 문서만 돌려준다 — 손대지 않은 파일을 다시 쓰지 않는다', () => {
    const changed = crossLinkCorpus([
      { id: 'DOC-1', label: 'GBX-ONB-001_계획서.docx', body: 'GBX-ONB-002 회의록 참조' },
      { id: 'DOC-2', label: 'GBX-ONB-002_회의록.docx', body: '참조 없음' },
    ]);
    expect([...changed.keys()]).toEqual(['DOC-1']);
    expect(changed.get('DOC-1')).toBe('[GBX-ONB-002](DOC-2.md) 회의록 참조');
  });

  it('참조가 없으면 아무것도 바꾸지 않는다', () => {
    expect(
      crossLinkCorpus([{ id: 'DOC-1', label: '일일업무.hwp', body: '오늘 업무 내용' }]).size,
    ).toBe(0);
  });
});
