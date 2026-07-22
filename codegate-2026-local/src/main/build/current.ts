/**
 * 로컬 위키 디렉터리의 레이아웃과 `current.json` 포인터 (스펙 v1.4 §5 — "위키 갱신은 원자적으로").
 *
 *   ~/.codegate/wiki/
 *     ├─ builds/<buildId>/     ← 불변 빌드 (조립이 끝난 뒤에만 이 이름을 얻는다)
 *     ├─ builds/.staging-…/    ← 조립 중인 임시 디렉터리 (실패하면 통째로 버린다)
 *     ├─ current.json          ← 정본 포인터. 이 파일이 바뀌는 순간이 곧 승격이다
 *     └─ current -> builds/…   ← 에이전트가 참조하는 안정 경로 (심볼릭 링크, 보조 수단)
 *
 * electron 을 import 하지 않는다 — `tests/build-atomic.test.ts` 가 이 모듈을 직접 돌린다.
 */
import path from 'node:path';
import { readJson } from '@main/util/fsx';

export const CURRENT_JSON = 'current.json';
export const BUILDS_DIR = 'builds';
export const CURRENT_LINK = 'current';
/** 빌드 안에서 재사용 판단에 쓰는 색인 파일 (스펙 v1.4 §5 — enrichment 비용 통제). */
export const BUILD_INDEX_JSON = 'index.json';
/** 빌드 안의 위키 매니페스트 — 에이전트가 읽는 문서 목록. */
export const BUILD_MANIFEST_JSON = 'manifest.json';

export interface CurrentPointer {
  buildId: string;
  promotedAt: string;
  /** 위키 디렉터리 기준 상대 경로 (`builds/<id>`) */
  dir: string;
  docCount: number;
  /** enrichment 가 보류된 문서 수 (스펙 v1.4 §5) */
  deferredCount: number;
}

/** 현재 승격된 빌드 포인터. 아직 빌드가 없으면 null. */
export function readCurrentPointer(wikiDir: string): Promise<CurrentPointer | null> {
  return readJson<CurrentPointer>(path.join(wikiDir, CURRENT_JSON));
}

/** 현재 빌드의 절대 경로. 없으면 null. */
export async function currentBuildDir(wikiDir: string): Promise<string | null> {
  const pointer = await readCurrentPointer(wikiDir);
  return pointer ? path.join(wikiDir, pointer.dir) : null;
}

export function buildDirFor(wikiDir: string, buildId: string): string {
  return path.join(wikiDir, BUILDS_DIR, buildId);
}

export function stagingDirFor(wikiDir: string, buildId: string): string {
  return path.join(wikiDir, BUILDS_DIR, `.staging-${buildId}`);
}
