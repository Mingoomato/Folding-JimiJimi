/**
 * L2 로컬 빌드 오케스트레이터 (스펙 v1.4 §1 L2).
 *
 *   변경 감지 → ① 변환(민규 모듈) → ② enrichment(LLM 직접 호출) → ③ 조립(용휘 빌더 코어)
 *   → 임시 디렉터리 검증 → 원자적 승격 → `current.json`
 *
 * v1.3 의 zip 패키징·업로드·폴링·산출물 회수·ACK 는 전부 사라졌다.
 * **문서는 자사 서버로 나가지 않는다.** 서버에 묻는 것은 로그인·구독뿐이고,
 * 그 확인은 빌드를 시작하기 전에 한 번 한다 (스펙 v1.4 §5 "구독 검사 시점").
 *
 * 상태 전이는 계약의 `BuildState` 그대로 렌더러에 밀어준다.
 * 실패는 반드시 한국어 문장으로 `BuildState.error` 에 담긴다 (스펙 v1.4 §5 silent fail 금지).
 */
import { randomUUID } from 'node:crypto';
import {
  AuthError,
  LLM_KEY_MISSING_MESSAGE,
  type BuildPhase,
  type BuildState,
  type BuildStatus,
} from '@contracts';
import type { FileRow, Store } from '@main/db/store';
import { UserFacingError, logError, toUserMessage } from '@main/util/errors';
import { toVirtualPath } from '@main/util/vpath';
import { promoteBuild, pruneOldBuilds, type BuilderCore } from './assemble';
import { PreviousBuild } from './cache';
import { runConversion, type DocumentConverter } from './convert';
import { runEnrichment, type Enricher } from './enrich';
import {
  BuildCancelledError,
  isCancellation,
  type DeferredDoc,
  type LlmKeyResolver,
  type SourceDoc,
} from './types';

/** 마지막으로 승격된 빌드 id — 오래된 빌드 정리에 쓴다. */
const KV_LAST_BUILD_ID = 'lastBuildId';

/**
 * 단계별 전체 진행률 구간.
 * 렌더러는 이 값으로 막대를, `phase.done/total` 로 "분석 12/37" 을 그린다.
 */
const PHASE_RANGE: Record<BuildPhase, [number, number]> = {
  convert: [5, 35],
  enrich: [35, 85],
  assemble: [85, 98],
};

const PHASE_STATUS: Record<BuildPhase, BuildStatus> = {
  convert: 'converting',
  enrich: 'enriching',
  assemble: 'assembling',
};

export interface BuildServiceOptions {
  store: Store;
  /** `~/.codegate/wiki` */
  wikiDir: string;
  /** ① 변환 모듈 (민규) — 실물이 오면 여기만 바뀐다 */
  converter: DocumentConverter;
  /** ② enrichment — LLM API 직접 호출 */
  enricher: Enricher;
  /** ③ 빌더 코어 (용휘) — 실물이 오면 여기만 바뀐다 */
  builder: BuilderCore;
  /** 자격증명 ② — 없으면 빌드를 시작하지 않는다 (스펙 v1.4 §5) */
  llmKeys: LlmKeyResolver;
  onState: (state: BuildState) => void;
  /** 파일 상태가 바뀌었을 때 트리를 다시 보내달라는 신호 */
  onFilesChanged: () => void;
  /** 빌드 시작 전 구독 확인. 401/402 면 AuthError 를 던진다 (스펙 v1.4 §5) */
  ensureSubscription?: () => Promise<void>;
  /** 401/402 를 세션에 반영하기 위한 훅 */
  onAuthError?: (error: AuthError) => void | Promise<void>;
  /** LLM 호출 동시성 (기본 4) */
  concurrency?: number;
}

export class BuildService {
  private state: BuildState = {
    buildId: null,
    status: 'idle',
    progress: 0,
    message: '대기 중',
    updatedAt: new Date().toISOString(),
  };

  private running = false;
  /** 실행 중 새 변경이 들어오면 끝난 뒤 한 번 더 돌린다. */
  private rerunRequested = false;
  /** legacy BuildService 내부 취소 스위치. sidecar 동기화 UI에는 노출하지 않는다. */
  private aborter: AbortController | null = null;
  /** 이번 빌드에서 보류된 문서들 (스펙 v1.4 §5). 매 emit 에 함께 실려 나간다. */
  private deferred: DeferredDoc[] = [];

  constructor(private readonly options: BuildServiceOptions) {}

  getState(): BuildState {
    return this.state;
  }

  /** 렌더러의 `build.trigger()` 와 워처 debounce flush 가 공유하는 진입점. */
  async trigger(): Promise<void> {
    if (this.running) {
      this.rerunRequested = true;
      return;
    }
    this.running = true;
    this.deferred = [];
    const aborter = new AbortController();
    this.aborter = aborter;

    try {
      await this.runOnce(aborter.signal);
    } catch (err) {
      if (isCancellation(err)) {
        // 취소는 실패가 아니다 — 다음 빌드에서 이어서 하도록 dirty 를 그대로 둔다.
        logError('build:cancelled', err);
        this.options.store.setStatusForDirty('pending');
        this.options.onFilesChanged();
        this.emit({
          status: 'idle',
          progress: 0,
          message: '빌드를 중단했습니다',
          phase: undefined,
          error: undefined,
        });
      } else {
        logError('build', err);
        if (err instanceof AuthError) await this.options.onAuthError?.(err);
        this.options.store.markDirtyAsError();
        this.options.onFilesChanged();
        this.emit({
          status: 'failed',
          progress: 0,
          message: '빌드 실패',
          phase: undefined,
          error: toUserMessage(err, '위키 빌드에 실패했습니다. 잠시 후 다시 시도해 주세요.'),
        });
      }
    } finally {
      this.running = false;
      this.aborter = null;
      if (this.rerunRequested) {
        this.rerunRequested = false;
        void this.trigger();
      }
    }
  }

  /**
   * legacy BuildService 호출자가 진행 중인 빌드를 멈출 때 쓴다.
   * enrichment 는 실제 비용이므로, 이 신호 이후로는 **새 LLM 호출이 나가지 않는다**
   * (워커가 다음 문서를 집어들기 직전에 `throwIfCancelled` 로 걸린다).
   */
  cancel(): void {
    this.rerunRequested = false;
    this.aborter?.abort();
  }

  private async runOnce(signal: AbortSignal): Promise<void> {
    const store = this.options.store;
    const roots = store.listRoots();
    if (roots.length === 0) {
      this.emit({
        status: 'idle',
        progress: 0,
        message: '등록된 폴더가 없습니다',
        phase: undefined,
        error: '먼저 문서 폴더를 등록해 주세요.',
      });
      return;
    }

    this.emit({
      buildId: null,
      status: 'scanning',
      progress: 1,
      message: '구독 상태를 확인하는 중',
      phase: undefined,
      error: undefined,
    });

    // 스펙 v1.4 §5 — 구독 검사 시점: 앱 시작 시 + **빌드 시작 전**.
    // 401/402 는 AuthError 로 올라와 한국어 문장으로 표시된다 (silent fail 금지).
    await this.options.ensureSubscription?.();

    // 자격증명 ② 가 없으면 시작조차 하지 않는다 (스펙 v1.4 §5).
    if (!(await this.options.llmKeys.resolve())) {
      throw new UserFacingError(LLM_KEY_MISSING_MESSAGE);
    }

    const files = store.listFiles();
    if (files.length === 0) {
      this.emit({ status: 'done', progress: 100, message: '분석할 문서가 없습니다', phase: undefined });
      return;
    }

    const dirty = store.listDirtyFiles();
    const deleted = store.listDeletedFiles();
    const previous = await PreviousBuild.load(this.options.wikiDir);
    if (dirty.length === 0 && deleted.length === 0 && previous.dir !== null) {
      this.emit({ status: 'done', progress: 100, message: '변경 사항 없음', phase: undefined });
      return;
    }

    store.setStatusForDirty('converting');
    this.options.onFilesChanged();

    const buildId = `bld_${Date.now().toString(36)}_${randomUUID().slice(0, 8)}`;
    const byVirtualPath = new Map<string, FileRow>();
    const sources: SourceDoc[] = files.map((file) => {
      const virtualPath = toVirtualPath(file.rootId, file.relPath);
      byVirtualPath.set(virtualPath, file);
      return {
        virtualPath,
        absPath: file.absPath,
        sha256: file.sha256,
        rootId: file.rootId,
        relPath: file.relPath,
      };
    });

    this.emit({ buildId, status: 'converting', progress: PHASE_RANGE.convert[0], message: '문서를 변환하는 중' });

    /* ------------------------------------------------------------ ① 변환 */
    const conversion = await runConversion({
      docs: sources,
      converter: this.options.converter,
      previous,
      signal,
      onProgress: (done, total) => this.emitPhase(buildId, 'convert', done, total, '문서를 변환하는 중'),
      onDeferred: (d) => this.addDeferred(d),
    });

    if (conversion.converted.length === 0) {
      throw new UserFacingError(
        '변환에 성공한 문서가 하나도 없어 위키를 만들지 못했습니다. 이전 위키를 그대로 유지했습니다.',
      );
    }

    /* ------------------------------------------- ② enrichment (LLM 호출) */
    this.emitPhase(buildId, 'enrich', 0, conversion.converted.length, '문서를 분석하는 중');
    const enrichment = await runEnrichment({
      docs: conversion.converted,
      enricher: this.options.enricher,
      previous,
      concurrency: this.options.concurrency,
      signal,
      onProgress: (done, total) => this.emitPhase(buildId, 'enrich', done, total, '문서를 분석하는 중'),
      onDeferred: (d) => this.addDeferred(d),
    });

    /* --------------------------------------------------- ③ 조립 + 승격 */
    this.emitPhase(buildId, 'assemble', 0, enrichment.enriched.length, '위키를 조립하는 중');
    const promoted = await promoteBuild({
      wikiDir: this.options.wikiDir,
      buildId,
      builder: this.options.builder,
      docs: enrichment.enriched,
      signal,
      onProgress: (done, total) => this.emitPhase(buildId, 'assemble', done, total, '위키를 조립하는 중'),
    });

    /* ------------------------------------------------------------ 마무리 */
    const previousBuildId = store.getKv(KV_LAST_BUILD_ID);
    store.setKv(KV_LAST_BUILD_ID, promoted.buildId);
    store.upsertBuild({
      id: promoted.buildId,
      status: 'done',
      progress: 100,
      message: '완료',
      error: null,
      deferred: this.deferred.length > 0 ? JSON.stringify(this.deferred) : null,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });

    // 성공한 문서는 dirty 해제, 보류된 문서만 dirty 로 남겨 다음 빌드에서 다시 시도한다.
    store.commitBuildResult();
    for (const item of this.deferred) {
      const row = byVirtualPath.get(item.path);
      if (row) store.markFileDeferred(row.rootId, row.relPath);
    }
    this.options.onFilesChanged();

    await pruneOldBuilds(
      this.options.wikiDir,
      [promoted.buildId, ...(previousBuildId ? [previousBuildId] : [])],
    ).catch((err: unknown) => logError('build:prune', err));

    this.emit({
      buildId: promoted.buildId,
      status: 'done',
      progress: 100,
      phase: undefined,
      message: summarize(enrichment.llmCalls, enrichment.reusedCount, this.deferred.length),
      error: undefined,
    });
  }

  private addDeferred(item: DeferredDoc): void {
    this.deferred = [...this.deferred, item];
  }

  /** 단계 진행을 전체 진행률로 환산해 흘려보낸다 ("분석 12/37"). */
  private emitPhase(
    buildId: string,
    name: BuildPhase,
    done: number,
    total: number,
    message: string,
  ): void {
    const [from, to] = PHASE_RANGE[name];
    const ratio = total > 0 ? Math.min(done / total, 1) : 1;
    this.emit({
      buildId,
      status: PHASE_STATUS[name],
      progress: Math.round(from + (to - from) * ratio),
      phase: { name, done, total },
      message,
    });
  }

  private emit(patch: Partial<BuildState>): void {
    this.state = {
      ...this.state,
      ...patch,
      // 보류 목록은 언제나 최신 상태로 함께 나간다 (스펙 v1.4 §5)
      deferred: this.deferred.length > 0 ? [...this.deferred] : undefined,
      updatedAt: new Date().toISOString(),
    };
    this.options.onState(this.state);
  }
}

function summarize(llmCalls: number, reused: number, deferred: number): string {
  const parts = [`위키 갱신 완료 (분석 ${llmCalls}건`];
  if (reused > 0) parts.push(`, 재사용 ${reused}건`);
  parts.push(')');
  if (deferred > 0) parts.push(` · 보류 ${deferred}건`);
  return parts.join('');
}

export { BuildCancelledError };
