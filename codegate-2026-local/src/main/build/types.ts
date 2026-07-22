/**
 * 로컬 빌드 파이프라인 공통 타입 (스펙 v1.4 §0 — 변환 → enrichment → 조립).
 *
 * v1.4 에서 서버는 로그인·구독만 담당한다. 문서는 자사 서버로 올라가지 않으며
 * 세 단계가 전부 이 컴퓨터에서 돈다. 그래서 이 모듈은 electron 도, 네트워크도 모른다 —
 * vitest 가 파이프라인을 그대로 돌릴 수 있어야 하기 때문이다.
 */
import { createHash } from 'node:crypto';
import type { BuildPhase, LlmProvider } from '@contracts';

/** 파이프라인 입력 — 등록 폴더에서 온 원본 문서 하나. */
export interface SourceDoc {
  /** 가상 경로 `<rootId>/<상대경로>` — 빌드 전체에서 문서를 식별하는 키 */
  virtualPath: string;
  absPath: string;
  /**
   * 원본 내용의 sha256.
   * enrichment 재사용 여부를 가르는 **유일한** 기준이다 (스펙 v1.4 §5 — 비용 통제).
   */
  sha256: string;
  rootId: string;
  relPath: string;
}

/** ① 변환 결과 — 변환 모듈이 뱉은 INPUT_CONTRACT 마크다운. */
export interface ConvertedDoc {
  virtualPath: string;
  docId: string;
  title: string;
  markdown: string;
  /** 이 마크다운을 만들어 낸 원본의 해시 */
  sourceSha256: string;
  /** 위키 문서 리비전 — 내용이 바뀔 때만 증가한다 (`【DOC-ID rev.N §섹션】`) */
  rev: number;
  /** 이전 빌드의 마크다운을 그대로 쓴 경우 true → 변환 모듈을 부르지 않았다 */
  reused: boolean;
}

/** ② enrichment 결과 — LLM 이 붙인 메타데이터. */
export interface Enrichment {
  summary: string;
  keywords: string[];
  sections: string[];
  aliases: string[];
  /** 어떤 모델이 만들었는지 — 감사·캐시 무효화 판단에 쓴다 */
  model: string;
}

export interface EnrichedDoc extends ConvertedDoc {
  enrichment: Enrichment;
  /** 이전 빌드의 enrichment 를 재사용한 경우 true → LLM 호출이 **없었다** */
  enrichmentReused: boolean;
  /** enrichment 가 실패해 보류된 경우 그 이유 (스펙 v1.4 §5) */
  deferredReason?: string;
}

/**
 * 보류된 문서 (스펙 v1.4 §5).
 * 한 문서의 enrichment 실패가 빌드 전체를 실패시켜서는 안 된다 —
 * 그 문서만 여기 기록하고 나머지는 계속 간다.
 */
export interface DeferredDoc {
  path: string;
  reason: string;
}

export interface PhaseProgress {
  name: BuildPhase;
  done: number;
  total: number;
}

/** 렌더러가 "분석 12/37" 을 그릴 수 있도록 단계 진행을 흘려보내는 콜백. */
export type PhaseReporter = (progress: PhaseProgress) => void;

/** enrichment 에 쓸 LLM 자격증명 (자격증명 ② — 스펙 v1.4 §5). */
export interface ResolvedLlmKey {
  provider: LlmProvider;
  /** 원문 키. **메인 프로세스 밖으로 절대 나가지 않는다.** */
  apiKey: string;
}

/**
 * 키 해석기 — 빌드 파이프라인은 저장 방식(safeStorage)을 알 필요가 없다.
 * 설정된 키가 하나도 없으면 null 을 돌려주고, 상위가 `LLM_KEY_MISSING_MESSAGE` 로 거절한다.
 */
export interface LlmKeyResolver {
  resolve(): Promise<ResolvedLlmKey | null>;
}

/**
 * legacy BuildService 내부 빌드 취소.
 * enrichment 는 실제 돈이 나가므로, 취소 신호가 오면 **새 LLM 호출을 더 내보내지 않는다**.
 */
export class BuildCancelledError extends Error {
  readonly cancelled = true;
  constructor(message = '빌드를 중단했습니다.') {
    super(message);
    this.name = 'BuildCancelledError';
  }
}

export function isCancellation(err: unknown): boolean {
  if (err instanceof BuildCancelledError) return true;
  return err instanceof Error && err.name === 'AbortError';
}

/** 다음 작업을 시작하기 직전에 부른다 — 취소되었으면 여기서 즉시 빠져나간다. */
export function throwIfCancelled(signal?: AbortSignal): void {
  if (signal?.aborted) throw new BuildCancelledError();
}

/** 취소 가능한 sleep — 취소 시 대기 중이던 백오프도 즉시 깨어난다. */
export function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new BuildCancelledError());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new BuildCancelledError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * 가상 경로 → 안정적인 문서 id.
 * 인용(`【DOC-ID rev.N §섹션】`)에 그대로 노출되므로 사람이 읽을 수 있게 만든다.
 */
export function makeDocId(virtualPath: string): string {
  const base = virtualPath.split('/').pop() ?? virtualPath;
  const stem = base.replace(/\.[^.]+$/, '');
  const slug =
    stem
      .replace(/[^\p{L}\p{N}]+/gu, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 24) || 'DOC';
  const digest = createHash('sha256').update(virtualPath).digest('hex').slice(0, 6);
  return `${slug}-${digest}`;
}

/** 마크다운 첫 `# ` 제목을 문서 제목으로 삼는다. 없으면 파일명. */
export function titleFromMarkdown(markdown: string, fallbackPath: string): string {
  const heading = /^#\s+(.+)$/m.exec(markdown);
  if (heading?.[1]) return heading[1].trim();
  const base = fallbackPath.split('/').pop() ?? fallbackPath;
  return base.replace(/\.[^.]+$/, '') || base;
}
