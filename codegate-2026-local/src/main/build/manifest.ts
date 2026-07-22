/**
 * 위키 매니페스트 (스펙 v1.4 §1 L2 — 빌더 코어가 내놓는 불변 빌드의 목차).
 *
 * v1.3 의 `files.json`(서버로 올리던 업로드 매니페스트)은 사라졌다. 이제 매니페스트는
 * **로컬 빌드 산출물 안에만** 존재하며, 에이전트(A2 `wiki_search`)가 읽는 문서 목록이다.
 */
import type { EnrichedDoc } from './types';

export interface WikiDocEntry {
  docId: string;
  /** 같은 문서라도 내용이 바뀌어 재빌드되면 증가 — 인용 `【DOC-ID rev.N §섹션】` 의 N */
  rev: number;
  title: string;
  /** 빌드 디렉터리 기준 마크다운 상대 경로 */
  file: string;
  /** 빌드 디렉터리 기준 enrichment JSON 상대 경로 */
  enrichmentFile: string;
  /** 원본의 가상 경로 — 인용을 클릭하면 이 경로로 원본을 연다 */
  sourcePath: string;
  sections: string[];
  aliases: string[];
  keywords: string[];
  summary: string;
  /** enrichment 가 보류된 문서 (스펙 v1.4 §5). 에이전트가 근거로 쓸 때 주의해야 한다. */
  deferred?: boolean;
}

export interface WikiManifest {
  wikiVersion: number;
  buildId: string;
  builtAt: string;
  docs: WikiDocEntry[];
}

export const WIKI_VERSION = 1;

export function docMarkdownPath(docId: string): string {
  return `docs/${docId}.md`;
}

export function docEnrichmentPath(docId: string): string {
  return `enrichment/${docId}.json`;
}

export function buildWikiManifest(input: {
  buildId: string;
  builtAt?: string;
  docs: EnrichedDoc[];
}): WikiManifest {
  return {
    wikiVersion: WIKI_VERSION,
    buildId: input.buildId,
    builtAt: input.builtAt ?? new Date().toISOString(),
    docs: input.docs.map((doc) => ({
      docId: doc.docId,
      rev: doc.rev,
      title: doc.title,
      file: docMarkdownPath(doc.docId),
      enrichmentFile: docEnrichmentPath(doc.docId),
      sourcePath: doc.virtualPath,
      sections: doc.enrichment.sections,
      aliases: doc.enrichment.aliases,
      keywords: doc.enrichment.keywords,
      summary: doc.enrichment.summary,
      ...(doc.deferredReason ? { deferred: true } : {}),
    })),
  };
}
