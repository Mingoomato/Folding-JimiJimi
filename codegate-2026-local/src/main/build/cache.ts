/**
 * 이전 빌드 재사용 캐시 (스펙 v1.4 §5 — "enrichment 비용 통제: 변경 문서만 재-enrichment (해시 비교)").
 *
 * 규칙은 하나다:
 *
 *   **가상 경로가 같고 원본 sha256 이 같으면 그 문서는 변하지 않았다 → LLM 을 다시 부르지 않는다.**
 *
 * 재사용 대상은 두 가지다.
 *   · 변환 결과(마크다운) — 변환 모듈을 다시 부르지 않는다
 *   · enrichment 결과      — **LLM 을 다시 부르지 않는다 (실제 비용)**
 *
 * 보류(deferred)된 문서는 캐시에 "미완성"으로 남겨 다음 빌드에서 반드시 재시도되게 한다.
 */
import path from 'node:path';
import { BUILD_INDEX_JSON, readCurrentPointer } from './current';
import { readJson } from '@main/util/fsx';
import fs from 'node:fs/promises';
import type { Enrichment } from './types';

/** `builds/<id>/index.json` 의 한 줄. */
export interface BuildIndexEntry {
  virtualPath: string;
  /** 이 빌드를 만들 때 원본이 갖고 있던 해시 */
  sourceSha256: string;
  docId: string;
  title: string;
  rev: number;
  /** 빌드 디렉터리 기준 상대 경로 */
  markdownFile: string;
  enrichmentFile: string;
  /** enrichment 가 보류된 채 담긴 문서 — 재사용 금지, 다음 빌드에서 다시 시도한다 */
  deferred?: boolean;
}

export interface BuildIndex {
  buildId: string;
  builtAt: string;
  entries: BuildIndexEntry[];
}

/**
 * 직전 승격 빌드에 대한 읽기 전용 뷰.
 * 이전 빌드가 없거나 색인이 깨졌으면 "아무것도 재사용할 수 없음" 상태로 동작한다.
 */
export class PreviousBuild {
  private readonly byPath: Map<string, BuildIndexEntry>;

  private constructor(
    /** 이전 빌드 디렉터리 절대 경로. 없으면 null. */
    readonly dir: string | null,
    entries: BuildIndexEntry[],
  ) {
    this.byPath = new Map(entries.map((e) => [e.virtualPath, e]));
  }

  static empty(): PreviousBuild {
    return new PreviousBuild(null, []);
  }

  static async load(wikiDir: string): Promise<PreviousBuild> {
    const pointer = await readCurrentPointer(wikiDir);
    if (!pointer) return PreviousBuild.empty();
    const dir = path.join(wikiDir, pointer.dir);
    const index = await readJson<BuildIndex>(path.join(dir, BUILD_INDEX_JSON));
    if (!index || !Array.isArray(index.entries)) return PreviousBuild.empty();
    return new PreviousBuild(dir, index.entries);
  }

  /** 같은 경로의 이전 항목 (해시가 달라도 rev 승계를 위해 필요하다). */
  entryFor(virtualPath: string): BuildIndexEntry | null {
    return this.byPath.get(virtualPath) ?? null;
  }

  /**
   * 재사용 가능한 항목 — 경로가 같고 해시도 같고 보류되지 않은 것만.
   * 이 함수가 null 을 돌려주면 그 문서는 변환·enrichment 를 다시 거친다.
   */
  reusable(virtualPath: string, sourceSha256: string): BuildIndexEntry | null {
    const entry = this.byPath.get(virtualPath);
    if (!entry) return null;
    if (entry.sourceSha256 !== sourceSha256) return null;
    if (entry.deferred) return null;
    return entry;
  }

  async readMarkdown(entry: BuildIndexEntry): Promise<string | null> {
    if (!this.dir) return null;
    try {
      return await fs.readFile(path.join(this.dir, entry.markdownFile), 'utf8');
    } catch {
      return null;
    }
  }

  async readEnrichment(entry: BuildIndexEntry): Promise<Enrichment | null> {
    if (!this.dir) return null;
    const parsed = await readJson<Enrichment>(path.join(this.dir, entry.enrichmentFile));
    if (!parsed || typeof parsed.summary !== 'string') return null;
    return {
      summary: parsed.summary,
      keywords: Array.isArray(parsed.keywords) ? parsed.keywords : [],
      sections: Array.isArray(parsed.sections) ? parsed.sections : [],
      aliases: Array.isArray(parsed.aliases) ? parsed.aliases : [],
      model: typeof parsed.model === 'string' ? parsed.model : 'unknown',
    };
  }
}
