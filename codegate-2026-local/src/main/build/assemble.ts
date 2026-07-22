/**
 * ③ 조립 단계 + 원자적 승격 (스펙 v1.4 §1 L2 · §2 "빌더 코어 라이브러리 — 용휘", §5).
 *
 * 이 파일이 지키는 단 하나의 불변식:
 *
 *   **새 빌드는 임시 디렉터리에 조립하고 검증까지 통과한 뒤에만
 *     `builds/<id>/` 로 승격하고 `current.json` 을 갱신한다.
 *     실패하면 이전 빌드와 `current.json` 은 손끝 하나 건드리지 않는다.**
 *
 * v1.3 의 다운로드·sha256 검증·ACK 는 없다 — 산출물이 네트워크를 건너오지 않기 때문이다.
 * 대신 "조립 결과가 스키마상 온전한가"를 검증한다.
 *
 * electron 을 import 하지 않는다 — `tests/build-atomic.test.ts` 가 이 모듈을 직접 돌린다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';
import { ensureDir, readJson, rmrf, writeJsonAtomic } from '@main/util/fsx';
import type { BuildIndex, BuildIndexEntry } from './cache';
import {
  BUILDS_DIR,
  BUILD_INDEX_JSON,
  BUILD_MANIFEST_JSON,
  CURRENT_JSON,
  CURRENT_LINK,
  buildDirFor,
  stagingDirFor,
  type CurrentPointer,
} from './current';
import {
  buildWikiManifest,
  docEnrichmentPath,
  docMarkdownPath,
  type WikiManifest,
} from './manifest';
import { throwIfCancelled, type EnrichedDoc } from './types';

/** 빌더 코어 npm 패키지 이름 (환경변수로 바꿔 끼울 수 있다). */
const BUILDER_PACKAGE = process.env.CODEGATE_BUILDER_MODULE ?? '@codegate/wiki-builder';

const BUILDER_MISSING =
  '위키 빌더 코어를 찾을 수 없습니다. 빌더 라이브러리가 아직 설치되지 않았습니다 — ' +
  '개발 중이라면 CODEGATE_MOCK=1 로 목 파이프라인을 사용해 주세요.';

export interface AssembleInput {
  buildId: string;
  /** 조립 결과를 쓸 디렉터리 (항상 임시 디렉터리다 — 승격은 상위가 한다) */
  outDir: string;
  docs: EnrichedDoc[];
  signal?: AbortSignal;
  onProgress?: (done: number, total: number) => void;
}

/** ③ 단계의 경계. 실물이 오면 `RealBuilderCore` 만 갈아 끼우면 된다. */
export interface BuilderCore {
  assemble(input: AssembleInput): Promise<void>;
}

/** 빌더 코어가 노출해야 할 최소 계약 (M0 인터페이스 — 우창 ↔ 용휘). */
export interface BuilderModule {
  assemble(input: {
    buildId: string;
    outDir: string;
    docs: { docId: string; rev: number; title: string; sourcePath: string; markdown: string; enrichment: unknown }[];
  }): Promise<void> | void;
}

let modulePromise: Promise<BuilderModule> | null = null;

export async function loadBuilderModule(): Promise<BuilderModule> {
  modulePromise ??= (async () => {
    let mod: { assemble?: unknown; default?: { assemble?: unknown } };
    try {
      mod = (await import(/* @vite-ignore */ BUILDER_PACKAGE)) as typeof mod;
    } catch (err) {
      throw new UserFacingError(BUILDER_MISSING, { cause: err });
    }
    const fn = mod.assemble ?? mod.default?.assemble;
    if (typeof fn !== 'function') {
      throw new UserFacingError(
        `${BUILDER_PACKAGE} 에서 assemble 을 찾지 못했습니다. 빌더 코어 버전을 확인해 주세요.`,
      );
    }
    return { assemble: fn as BuilderModule['assemble'] };
  })();

  try {
    return await modulePromise;
  } catch (err) {
    modulePromise = null;
    throw err;
  }
}

/** 실물 빌더 코어(용휘 라이브러리) 호출. 없으면 조용히 넘어가지 않고 크게 실패한다. */
export class RealBuilderCore implements BuilderCore {
  async assemble(input: AssembleInput): Promise<void> {
    throwIfCancelled(input.signal);
    const mod = await loadBuilderModule();
    await mod.assemble({
      buildId: input.buildId,
      outDir: input.outDir,
      docs: input.docs.map((d) => ({
        docId: d.docId,
        rev: d.rev,
        title: d.title,
        sourcePath: d.virtualPath,
        markdown: d.markdown,
        enrichment: d.enrichment,
      })),
    });
    input.onProgress?.(input.docs.length, input.docs.length);
    // 빌더가 무엇을 남겼든 아래 `validateBuild` 가 승격 전에 다시 검사한다.
  }
}

/**
 * 목 빌더 코어 (`CODEGATE_MOCK=1`).
 * 실물이 내놓을 레이아웃을 그대로 만든다 — `manifest.json` + `docs/` + `enrichment/`.
 */
export class MockBuilderCore implements BuilderCore {
  constructor(private readonly delayMs = 0) {}

  async assemble(input: AssembleInput): Promise<void> {
    await ensureDir(path.join(input.outDir, 'docs'));
    await ensureDir(path.join(input.outDir, 'enrichment'));

    let done = 0;
    for (const doc of input.docs) {
      throwIfCancelled(input.signal);
      await fs.writeFile(
        path.join(input.outDir, docMarkdownPath(doc.docId)),
        doc.markdown,
        'utf8',
      );
      await writeJsonAtomic(
        path.join(input.outDir, docEnrichmentPath(doc.docId)),
        doc.enrichment,
      );
      done += 1;
      input.onProgress?.(done, input.docs.length);
      if (this.delayMs > 0) await new Promise((r) => setTimeout(r, this.delayMs));
    }

    const manifest = buildWikiManifest({ buildId: input.buildId, docs: input.docs });
    await writeJsonAtomic(path.join(input.outDir, BUILD_MANIFEST_JSON), manifest);
  }
}

export function createBuilderCore(options: { mock?: boolean; mockDelayMs?: number }): BuilderCore {
  return options.mock ? new MockBuilderCore(options.mockDelayMs ?? 0) : new RealBuilderCore();
}

/* ============================================================================
 * 검증 → 승격
 * ========================================================================== */

/**
 * 조립 결과가 온전한지 확인한다 (스펙 v1.4 §5 — "스키마 검증 통과 시에만 current.json 갱신").
 * 여기서 던지면 승격은 일어나지 않고 이전 빌드가 그대로 남는다.
 */
export async function validateBuild(dir: string, expectedDocIds: string[]): Promise<WikiManifest> {
  const manifest = await readJson<WikiManifest>(path.join(dir, BUILD_MANIFEST_JSON));
  if (!manifest || !Array.isArray(manifest.docs)) {
    throw new UserFacingError(
      '새로 만든 위키에 목차(manifest.json)가 없습니다. 이전 위키를 그대로 유지했습니다.',
    );
  }

  const present = new Set(manifest.docs.map((d) => d.docId));
  const missing = expectedDocIds.filter((id) => !present.has(id));
  if (missing.length > 0) {
    throw new UserFacingError(
      `새로 만든 위키에서 문서 ${missing.length}건이 빠졌습니다. 이전 위키를 그대로 유지했습니다.`,
    );
  }

  for (const doc of manifest.docs) {
    if (!doc.docId || !doc.file) {
      throw new UserFacingError(
        '새로 만든 위키의 목차가 손상되었습니다. 이전 위키를 그대로 유지했습니다.',
      );
    }
    const file = path.resolve(dir, doc.file);
    if (!file.startsWith(path.resolve(dir))) {
      throw new UserFacingError(`위키 목차에 허용되지 않은 경로가 있습니다: ${doc.file}`);
    }
    const stat = await fs.stat(file).catch(() => null);
    if (!stat?.isFile()) {
      throw new UserFacingError(
        `새로 만든 위키에서 「${doc.title || doc.docId}」 본문을 찾지 못했습니다. 이전 위키를 그대로 유지했습니다.`,
      );
    }
  }

  return manifest;
}

export interface PromoteBuildOptions {
  /** `~/.codegate/wiki` */
  wikiDir: string;
  buildId: string;
  builder: BuilderCore;
  docs: EnrichedDoc[];
  signal?: AbortSignal;
  onProgress?: (done: number, total: number) => void;
}

export interface PromoteBuildResult {
  buildId: string;
  /** 승격된 절대 경로 (`<wikiDir>/builds/<id>`) */
  dir: string;
  manifest: WikiManifest;
  pointer: CurrentPointer;
}

/**
 * 조립 → 검증 → 승격 → `current.json` 갱신.
 *
 * 순서가 곧 안전장치다. ①②가 실패하면 임시 디렉터리만 지우고 끝내므로,
 * 이전 빌드와 `current.json` 은 아무 일도 없었던 것처럼 남는다 (스펙 v1.4 §5).
 */
export async function promoteBuild(options: PromoteBuildOptions): Promise<PromoteBuildResult> {
  const { wikiDir, buildId, docs, signal } = options;
  const finalDir = buildDirFor(wikiDir, buildId);
  const stagingDir = stagingDirFor(wikiDir, buildId);

  await ensureDir(path.join(wikiDir, BUILDS_DIR));
  await rmrf(stagingDir);
  await ensureDir(stagingDir);

  let manifest: WikiManifest;
  try {
    // ① 임시 디렉터리에 조립 — 여기서 무엇이 터져도 기존 위키는 무사하다
    await options.builder.assemble({
      buildId,
      outDir: stagingDir,
      docs,
      signal,
      onProgress: options.onProgress,
    });

    // ② 검증 — 통과하지 못하면 아래 어느 줄도 실행되지 않는다
    manifest = await validateBuild(
      stagingDir,
      docs.map((d) => d.docId),
    );

    // 다음 빌드가 재사용 판단에 쓸 색인 (스펙 v1.4 §5 — 비용 통제)
    await writeJsonAtomic(path.join(stagingDir, BUILD_INDEX_JSON), makeIndex(buildId, docs));
  } catch (err) {
    await rmrf(stagingDir);
    throw err;
  }

  // ③ 승격 — rename 은 원자적이다. 이 줄을 지나야 비로소 새 빌드가 이름을 얻는다.
  await rmrf(finalDir);
  await fs.rename(stagingDir, finalDir);

  // ④ 정본 포인터 갱신 — 원자적 쓰기. 이 순간이 "새 위키로 교체"다.
  const pointer: CurrentPointer = {
    buildId,
    promotedAt: new Date().toISOString(),
    dir: `${BUILDS_DIR}/${buildId}`,
    docCount: docs.length,
    deferredCount: docs.filter((d) => d.deferredReason).length,
  };
  await writeJsonAtomic(path.join(wikiDir, CURRENT_JSON), pointer);
  await updateCurrentLink(wikiDir, buildId);

  return { buildId, dir: finalDir, manifest, pointer };
}

function makeIndex(buildId: string, docs: EnrichedDoc[]): BuildIndex {
  const entries: BuildIndexEntry[] = docs.map((doc) => ({
    virtualPath: doc.virtualPath,
    sourceSha256: doc.sourceSha256,
    docId: doc.docId,
    title: doc.title,
    rev: doc.rev,
    markdownFile: docMarkdownPath(doc.docId),
    enrichmentFile: docEnrichmentPath(doc.docId),
    // 보류된 문서는 재사용 금지 — 다음 빌드에서 반드시 다시 분석한다
    ...(doc.deferredReason ? { deferred: true } : {}),
  }));
  return { buildId, builtAt: new Date().toISOString(), entries };
}

/**
 * `<wikiDir>/current` 심볼릭 링크를 새 빌드로 옮긴다 (에이전트가 쓰는 안정 경로).
 * 심볼릭 링크가 막힌 환경에서는 조용히 건너뛴다 — `current.json` 이 정본이다.
 */
async function updateCurrentLink(wikiDir: string, buildId: string): Promise<void> {
  const linkPath = path.join(wikiDir, CURRENT_LINK);
  const tmpLink = path.join(wikiDir, `.${CURRENT_LINK}.tmp`);
  try {
    await fs.rm(tmpLink, { force: true });
    await fs.symlink(path.join(BUILDS_DIR, buildId), tmpLink, 'dir');
    await fs.rename(tmpLink, linkPath);
  } catch {
    await fs.rm(tmpLink, { force: true }).catch(() => undefined);
  }
}

/** 오래된 빌드 정리 — 최근 `keep` 개만 남긴다 (현재 빌드는 항상 보존). */
export async function pruneOldBuilds(
  wikiDir: string,
  keepBuildIds: string[],
  keep = 3,
): Promise<void> {
  const buildsRoot = path.join(wikiDir, BUILDS_DIR);
  let names: string[];
  try {
    names = await fs.readdir(buildsRoot);
  } catch {
    return;
  }
  const keepSet = new Set(keepBuildIds);
  const candidates = names.filter((n) => !n.startsWith('.') && !keepSet.has(n)).sort();
  const doomed = candidates.slice(0, Math.max(0, candidates.length - keep));
  for (const name of doomed) await rmrf(path.join(buildsRoot, name));
}
