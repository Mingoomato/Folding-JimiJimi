/**
 * L3 진입점 — 실물/목 선택 (스펙 v1.3 §4 "M0 이후 mock 으로 병렬 진행").
 * `CODEGATE_MOCK=1` 이면 `MockKordoc`, 아니면 `RealKordoc`.
 */
import type { KordocApi } from '@contracts';
import { MockKordoc } from './mock';
import { RealKordoc } from './real';

export { KordocBase } from './base';
export { MockKordoc, MOCK_UNAPPLIED_MARK } from './mock';
export { RealKordoc } from './real';
export { atomicReplaceFile } from './atomic';
export type { AtomicReplaceHooks } from './atomic';

/** 목 모드 여부 — 빌드 백엔드/에이전트 선택과 동일한 스위치를 쓴다. */
export function isMockMode(): boolean {
  return process.env.CODEGATE_MOCK === '1';
}

export interface CreateKordocOptions {
  /** 목 모드에서 parse 픽스처를 찾을 디렉터리 */
  fixtureDir?: string;
}

export function createKordoc(options: CreateKordocOptions = {}): KordocApi {
  return isMockMode() ? new MockKordoc({ fixtureDir: options.fixtureDir }) : new RealKordoc();
}
