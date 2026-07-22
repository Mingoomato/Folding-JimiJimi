/**
 * 문서끼리의 **텍스트 참조를 마크다운 링크로 바꾼다.**
 *
 * 왜 필요한가
 * ----------
 * LLMWIKI 빌더는 관계(그래프 간선)를 `[제목](DOC-xxxx.md)` 형태의 **하이퍼링크에서만**
 * 뽑아낸다. 위키처럼 이미 엮인 문서를 전제한 설계다.
 *
 * 그런데 우리 입력은 docx·pdf·hwp 를 변환한 것이라 하이퍼링크가 없다. 문서들은
 * `GBX-ONB-001` 같은 **문서 번호를 글로** 가리킬 뿐이다. 실제로 상호 참조가 140군데
 * 있는데도 하이퍼링크가 0이라 링크 후보가 0이 되고, 그래서 간선이 하나도 생기지 않았다.
 *
 * 여기서 그 간극을 메운다. 빌더의 규칙(명시적 링크만 인정)은 그대로 두고, 변환 단계에서
 * 이미 존재하는 참조를 빌더가 알아볼 수 있는 형태로 적어 줄 뿐이다.
 *
 * 무엇을 연결하지 않는가
 * --------------------
 * 없는 관계를 만들어 내면 그래프가 거짓말을 한다. 그래서 **아주 좁게** 잡는다.
 *   · 문서 번호처럼 생긴 것만 (영문 대문자로 시작, 하이픈으로 이어지고, 숫자를 포함)
 *   · 코퍼스 안에서 **정확히 한 문서**를 가리킬 때만 — 둘 이상이면 건드리지 않는다
 *   · 자기 자신은 연결하지 않는다
 *   · 이미 링크 안에 있는 글자는 두 번 감싸지 않는다
 */

/**
 * 문서 번호처럼 생긴 토큰.
 *
 * `GBX-ONB-001`, `DOC-2026-07` 같은 것을 잡고 `2026-07-22`(날짜)나 `UTF-8` 은 거른다 —
 * 앞이 영문 대문자로 시작하고, 하이픈 묶음이 하나 이상이며, 숫자를 포함해야 한다.
 */
/*
 * `\b` 를 쓰면 안 된다. `_` 가 단어 문자라서 `GBX-ONB-001_고객온보딩_계획서.docx` 처럼
 * 밑줄로 이어 붙인 파일명에서 번호 끝의 경계가 성립하지 않는다(실제로 하나도 못 잡았다).
 * 그래서 "번호를 이룰 수 있는 글자"가 앞뒤에 없을 것만 요구한다.
 */
const CODE_RE = /(?<![A-Z0-9-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4}(?![A-Z0-9-])/g;

/** 이미 링크인 부분 — 여기 안의 글자는 건드리지 않는다. */
const MARKDOWN_LINK_RE = /\[[^\]]*\]\([^)]*\)/g;

const MIN_CODE_LENGTH = 6;

/** 파일명·제목에서 이 문서를 가리키는 번호를 뽑는다. */
export function documentCodes(text: string): string[] {
  const found = text.toUpperCase().match(CODE_RE) ?? [];
  return [...new Set(found)].filter((code) => code.length >= MIN_CODE_LENGTH && /[0-9]/.test(code));
}

export interface CrossLinkDoc {
  /** 정규화 입력의 문서 id (`DOC-...`) — 링크 대상 파일명이 된다 */
  id: string;
  /** 번호를 뽑아낼 원본 파일명(또는 제목) */
  label: string;
  body: string;
}

/**
 * 코퍼스 전체에서 **유일하게 한 문서만** 가리키는 번호를 고른다.
 *
 * 같은 번호를 여러 문서가 주장하면 어느 쪽인지 알 수 없다. 그때는 찍지 않고 버린다 —
 * 잘못 이은 간선은 없는 간선보다 나쁘다.
 */
export function uniqueCodeIndex(docs: CrossLinkDoc[]): Map<string, string> {
  const owners = new Map<string, Set<string>>();
  for (const doc of docs) {
    for (const code of documentCodes(doc.label)) {
      const set = owners.get(code) ?? new Set<string>();
      set.add(doc.id);
      owners.set(code, set);
    }
  }
  const index = new Map<string, string>();
  for (const [code, ids] of owners) {
    if (ids.size === 1) index.set(code, [...ids][0]);
  }
  return index;
}

/** 한 문서의 본문에서 다른 문서를 가리키는 번호를 링크로 감싼다. */
export function linkBody(body: string, index: Map<string, string>, selfId: string): string {
  if (index.size === 0) return body;

  // 이미 링크인 구간은 그대로 통과시키고, 그 사이의 평문에서만 치환한다.
  const segments: string[] = [];
  let cursor = 0;
  MARKDOWN_LINK_RE.lastIndex = 0;
  for (let m = MARKDOWN_LINK_RE.exec(body); m !== null; m = MARKDOWN_LINK_RE.exec(body)) {
    segments.push(linkPlain(body.slice(cursor, m.index), index, selfId), m[0]);
    cursor = m.index + m[0].length;
  }
  segments.push(linkPlain(body.slice(cursor), index, selfId));
  return segments.join('');
}

function linkPlain(text: string, index: Map<string, string>, selfId: string): string {
  CODE_RE.lastIndex = 0;
  return text.replace(CODE_RE, (code) => {
    const target = index.get(code.toUpperCase());
    // 대상이 없거나, 자기 자신을 가리키면 원문 그대로 둔다.
    return target && target !== selfId ? `[${code}](${target}.md)` : code;
  });
}

/**
 * 코퍼스 전체에 링크를 넣는다. 바뀐 문서만 돌려준다 — 바뀌지 않은 파일은 다시 쓰지 않는다.
 */
export function crossLinkCorpus(docs: CrossLinkDoc[]): Map<string, string> {
  const index = uniqueCodeIndex(docs);
  const changed = new Map<string, string>();
  for (const doc of docs) {
    const linked = linkBody(doc.body, index, doc.id);
    if (linked !== doc.body) changed.set(doc.id, linked);
  }
  return changed;
}
