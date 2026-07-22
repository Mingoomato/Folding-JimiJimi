/**
 * ② enrichment 단계 (스펙 v1.4 §0 · §1 L2 — "enrichment = LLM API 직접 호출").
 *
 * v1.4 에서 이 단계는 **이 컴퓨터에서** LLM 제공자에게 직접 붙는다. 자사 서버는 끼지 않는다.
 * 그래서 여기가 파이프라인에서 유일하게 실제 돈이 나가는 곳이고, 스펙 v1.4 §5 가 요구하는
 * 세 가지 안전장치가 전부 이 파일에 있다.
 *
 *   1. 동시성 제한  — 기본 4, `CODEGATE_ENRICH_CONCURRENCY` 로 조정
 *   2. 재시도       — 429/5xx/네트워크 오류만 지수 백오프로 재시도
 *   3. 부분 실패 격리 — 한 문서가 끝내 실패해도 빌드를 죽이지 않고 그 문서만 보류(deferred)
 *
 * 그리고 비용 통제의 핵심: **해시가 같은 문서는 아예 호출하지 않는다** (`PreviousBuild.reusable`).
 *
 * 자격증명 ②(enrichment LLM 키)만 쓴다. ①백엔드 토큰·③Anthropic SDK 자격증명과 섞지 않는다.
 */
import { LLM_PROVIDER_LABEL, type LlmProvider } from '@contracts';
import { UserFacingError } from '@main/util/errors';
import type { PreviousBuild } from './cache';
import {
  BuildCancelledError,
  delay,
  isCancellation,
  throwIfCancelled,
  type ConvertedDoc,
  type DeferredDoc,
  type EnrichedDoc,
  type Enrichment,
  type LlmKeyResolver,
  type ResolvedLlmKey,
} from './types';

/** 스펙 v1.4 §5 — 동시성 제한 기본값. */
export const DEFAULT_ENRICH_CONCURRENCY = 4;
/** 재시도 총 시도 횟수 (첫 호출 포함). */
export const DEFAULT_MAX_ATTEMPTS = 4;

/** ② 단계의 경계. 문서 하나를 enrich 한다. 실패는 throw — 보류 처리는 상위가 한다. */
export interface Enricher {
  enrich(doc: ConvertedDoc, signal?: AbortSignal): Promise<Enrichment>;
}

/** 429/5xx/네트워크 — 다시 걸면 성공할 수 있는 오류. */
export class RetryableLlmError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'RetryableLlmError';
  }
}

/* ============================================================================
 * 목 enrichment — `CODEGATE_MOCK=1`. 네트워크를 전혀 쓰지 않는다.
 * ========================================================================== */

/**
 * 목 모드에서 "이 문서만 실패" 시나리오를 재현하는 표식.
 * 변환된 마크다운 안에 이 문자열이 있으면 해당 문서만 보류된다 (스펙 v1.4 §5 부분 실패).
 */
export const MOCK_ENRICH_FAIL_MARK = '<!-- enrich:fail -->';

export class MockEnricher implements Enricher {
  constructor(
    /** 진행률이 눈에 보이도록 넣는 인위적 지연 (스펙 v1.4 §3 "분석 12/37") */
    private readonly delayMs = 180,
  ) {}

  async enrich(doc: ConvertedDoc, signal?: AbortSignal): Promise<Enrichment> {
    throwIfCancelled(signal);
    if (this.delayMs > 0) await delay(this.delayMs, signal);
    if (doc.markdown.includes(MOCK_ENRICH_FAIL_MARK)) {
      throw new UserFacingError('목 enrichment 가 이 문서를 처리하지 못했습니다.');
    }
    return {
      summary: `${doc.title} 문서의 핵심 내용을 요약한 자리표시자입니다.`,
      keywords: extractKeywords(doc.markdown),
      sections: extractSections(doc.markdown),
      aliases: [doc.title],
      model: 'mock-enricher',
    };
  }
}

/* ============================================================================
 * 실물 enrichment — LLM API 직접 호출
 * ========================================================================== */

/** 기본 모델. 조립 라이브러리(용휘)의 prompts/schemas 가 확정되면 함께 갈아 끼운다. */
const DEFAULT_MODEL: Record<LlmProvider, string> = {
  gemini: 'gemini-2.0-flash',
  openai: 'gpt-4o-mini',
  anthropic: 'claude-3-5-haiku-latest',
};

/**
 * enrichment 프롬프트.
 * 스펙 v1.4 §2 대로 최종적으로는 기존 wiki-builder `prompts/`·`schemas/` 를 그대로 재사용한다 —
 * 그때까지는 같은 출력 스키마를 유지하는 최소 프롬프트를 쓴다.
 */
const SYSTEM_PROMPT = [
  '당신은 사내 문서 위키의 색인 작성자입니다.',
  '주어진 마크다운 문서를 읽고 아래 JSON 스키마로만 답하십시오. 다른 말은 쓰지 마십시오.',
  '{"summary": string, "keywords": string[], "sections": string[], "aliases": string[]}',
  '- summary: 문서 전체를 3문장 이내 한국어로 요약',
  '- keywords: 검색어로 쓸 핵심 용어 최대 10개',
  '- sections: 문서의 주요 섹션 제목 (원문 표기 그대로)',
  '- aliases: 이 문서를 부를 수 있는 다른 이름 최대 5개',
  '문서에 없는 내용을 지어내지 마십시오.',
].join('\n');

export interface LlmEnricherOptions {
  keys: LlmKeyResolver;
  /** provider 별 모델 override */
  model?: string;
  maxAttempts?: number;
  /** 한 호출의 제한 시간(ms) */
  timeoutMs?: number;
  /** 테스트용 fetch 주입 */
  fetchImpl?: typeof fetch;
}

export class LlmEnricher implements Enricher {
  private readonly maxAttempts: number;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: LlmEnricherOptions) {
    this.maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
    this.timeoutMs = options.timeoutMs ?? 60_000;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async enrich(doc: ConvertedDoc, signal?: AbortSignal): Promise<Enrichment> {
    throwIfCancelled(signal);
    const key = await this.options.keys.resolve();
    if (!key) {
      // 상위(BuildService)가 빌드 시작 전에 이미 막지만, 방어적으로 한 번 더.
      throw new UserFacingError('enrichment LLM 키가 설정되지 않았습니다.');
    }
    const model = this.options.model ?? DEFAULT_MODEL[key.provider];
    const text = await this.callWithRetry(key, model, buildUserPrompt(doc), signal);
    return { ...parseEnrichment(text), model: `${key.provider}:${model}` };
  }

  /** 429/5xx 만 지수 백오프로 재시도한다 (스펙 v1.4 §5). */
  private async callWithRetry(
    key: ResolvedLlmKey,
    model: string,
    prompt: string,
    signal?: AbortSignal,
  ): Promise<string> {
    let attempt = 0;
    for (;;) {
      attempt += 1;
      throwIfCancelled(signal);
      try {
        return await this.call(key, model, prompt, signal);
      } catch (err) {
        if (isCancellation(err)) throw err;
        if (!(err instanceof RetryableLlmError) || attempt >= this.maxAttempts) throw err;
        // 1s → 2s → 4s (+지터). 취소되면 대기 도중에도 즉시 깨어난다.
        const backoff = 1000 * 2 ** (attempt - 1) + Math.floor(Math.random() * 250);
        await delay(backoff, signal);
      }
    }
  }

  private async call(
    key: ResolvedLlmKey,
    model: string,
    prompt: string,
    signal?: AbortSignal,
  ): Promise<string> {
    const controller = new AbortController();
    const onAbort = (): void => controller.abort();
    signal?.addEventListener('abort', onAbort, { once: true });
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const request = buildRequest(key, model, prompt);
      let response: Response;
      try {
        response = await this.fetchImpl(request.url, {
          method: 'POST',
          headers: request.headers,
          body: JSON.stringify(request.body),
          signal: controller.signal,
        });
      } catch (err) {
        if (signal?.aborted) throw new BuildCancelledError();
        // 네트워크 오류·타임아웃은 재시도 대상이다
        throw new RetryableLlmError(
          `${LLM_PROVIDER_LABEL[key.provider]} 에 연결하지 못했습니다.`,
          undefined,
        );
      }

      if (!response.ok) {
        const detail = (await response.text().catch(() => '')).slice(0, 300);
        if (response.status === 429 || response.status >= 500) {
          throw new RetryableLlmError(
            `${LLM_PROVIDER_LABEL[key.provider]} 응답 ${response.status}`,
            response.status,
          );
        }
        if (response.status === 401 || response.status === 403) {
          throw new UserFacingError(
            `${LLM_PROVIDER_LABEL[key.provider]} API 키가 거부되었습니다. 설정에서 키를 다시 확인해 주세요.`,
          );
        }
        throw new UserFacingError(
          `${LLM_PROVIDER_LABEL[key.provider]} 문서 분석 실패 (응답 ${response.status}) ${detail}`,
        );
      }

      const payload = (await response.json()) as unknown;
      return request.extract(payload);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
  }
}

interface ProviderRequest {
  url: string;
  headers: Record<string, string>;
  body: unknown;
  extract: (payload: unknown) => string;
}

/**
 * provider 별 요청 형태.
 * `anthropic` 이 여기 있어도 그것은 **자격증명 ②** 로 부르는 enrichment 호출이다 —
 * 에이전트 추론용 자격증명 ③(환경변수)과는 완전히 별개의 경로다 (스펙 v1.4 §5).
 */
function buildRequest(key: ResolvedLlmKey, model: string, prompt: string): ProviderRequest {
  switch (key.provider) {
    case 'gemini':
      return {
        url: `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`,
        headers: { 'content-type': 'application/json', 'x-goog-api-key': key.apiKey },
        body: {
          systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
          contents: [{ role: 'user', parts: [{ text: prompt }] }],
          generationConfig: { temperature: 0, responseMimeType: 'application/json' },
        },
        extract: (payload) =>
          pick(payload, ['candidates', 0, 'content', 'parts', 0, 'text']) ??
          fail('Gemini 응답에서 본문을 찾지 못했습니다.'),
      };
    case 'openai':
      return {
        url: 'https://api.openai.com/v1/chat/completions',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${key.apiKey}` },
        body: {
          model,
          temperature: 0,
          response_format: { type: 'json_object' },
          messages: [
            { role: 'system', content: SYSTEM_PROMPT },
            { role: 'user', content: prompt },
          ],
        },
        extract: (payload) =>
          pick(payload, ['choices', 0, 'message', 'content']) ??
          fail('OpenAI 응답에서 본문을 찾지 못했습니다.'),
      };
    case 'anthropic':
      return {
        url: 'https://api.anthropic.com/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-api-key': key.apiKey,
          'anthropic-version': '2023-06-01',
        },
        body: {
          model,
          max_tokens: 2048,
          temperature: 0,
          system: SYSTEM_PROMPT,
          messages: [{ role: 'user', content: prompt }],
        },
        extract: (payload) =>
          pick(payload, ['content', 0, 'text']) ??
          fail('Anthropic 응답에서 본문을 찾지 못했습니다.'),
      };
  }
}

function pick(payload: unknown, keys: (string | number)[]): string | null {
  let cursor: unknown = payload;
  for (const key of keys) {
    if (cursor === null || typeof cursor !== 'object') return null;
    cursor = (cursor as Record<string | number, unknown>)[key];
  }
  return typeof cursor === 'string' ? cursor : null;
}

function fail(message: string): never {
  throw new UserFacingError(message);
}

/** 모델이 코드펜스로 감싸 주는 경우까지 감안해 JSON 을 건져낸다. */
export function parseEnrichment(text: string): Omit<Enrichment, 'model'> {
  const stripped = text.trim().replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '');
  const start = stripped.indexOf('{');
  const end = stripped.lastIndexOf('}');
  if (start < 0 || end <= start) {
    throw new UserFacingError('문서 분석 결과를 이해하지 못했습니다 (JSON 형식이 아닙니다).');
  }
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(stripped.slice(start, end + 1)) as Record<string, unknown>;
  } catch (err) {
    throw new UserFacingError('문서 분석 결과를 이해하지 못했습니다 (JSON 파싱 실패).', {
      cause: err,
    });
  }
  return {
    summary: typeof parsed.summary === 'string' ? parsed.summary : '',
    keywords: toStringArray(parsed.keywords),
    sections: toStringArray(parsed.sections),
    aliases: toStringArray(parsed.aliases),
  };
}

function toStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function buildUserPrompt(doc: ConvertedDoc): string {
  // 토큰 폭주를 막기 위해 앞부분만 보낸다 — 요약·색인에는 충분하다.
  const body = doc.markdown.slice(0, 24_000);
  return `문서 제목: ${doc.title}\n문서 경로: ${doc.virtualPath}\n\n---\n${body}`;
}

/* ============================================================================
 * 오케스트레이션 — 캐시 재사용 · 동시성 제한 · 부분 실패 격리 · 취소
 * ========================================================================== */

export interface RunEnrichmentOptions {
  docs: ConvertedDoc[];
  enricher: Enricher;
  /** 직전 승격 빌드 — 해시가 같은 문서의 enrichment 를 그대로 물려받는다 */
  previous: PreviousBuild;
  concurrency?: number;
  signal?: AbortSignal;
  /** "분석 12/37" 을 위한 진행 보고 */
  onProgress?: (done: number, total: number) => void;
  onDeferred?: (deferred: DeferredDoc) => void;
}

export interface RunEnrichmentResult {
  enriched: EnrichedDoc[];
  deferred: DeferredDoc[];
  /** 실제로 LLM 을 부른 문서 수 — 비용 통제가 작동하는지 확인하는 지표 */
  llmCalls: number;
  /** 이전 빌드에서 그대로 가져온 문서 수 */
  reusedCount: number;
}

/**
 * 문서별 enrichment 를 돌린다.
 *
 * - 해시가 같은 문서는 이전 빌드 결과를 그대로 쓴다 → **LLM 호출 0회**
 * - 실패한 문서는 보류 자리표시자를 달고 빌드에 남는다 → 빌드 전체는 계속 완료된다
 * - 취소되면 그 시점 이후로 **새 호출을 하나도 내보내지 않는다**
 */
export async function runEnrichment(options: RunEnrichmentOptions): Promise<RunEnrichmentResult> {
  const { docs, enricher, previous, signal } = options;
  const concurrency = Math.max(1, options.concurrency ?? DEFAULT_ENRICH_CONCURRENCY);
  const enriched: EnrichedDoc[] = new Array<EnrichedDoc>(docs.length);
  const deferred: DeferredDoc[] = [];
  let llmCalls = 0;
  let reusedCount = 0;
  let done = 0;

  const complete = (index: number, value: EnrichedDoc): void => {
    enriched[index] = value;
    done += 1;
    options.onProgress?.(done, docs.length);
  };

  await mapConcurrent(docs, concurrency, async (doc, index) => {
    // 새 작업을 집어들기 직전에 취소를 확인한다 — 취소 후 호출이 새로 나가지 않는 지점.
    throwIfCancelled(signal);

    // ── 비용 통제: 해시가 같으면 이전 빌드의 enrichment 를 그대로 (스펙 v1.4 §5)
    const cached = previous.reusable(doc.virtualPath, doc.sourceSha256);
    if (cached) {
      const reusedEnrichment = await previous.readEnrichment(cached);
      if (reusedEnrichment) {
        reusedCount += 1;
        complete(index, { ...doc, enrichment: reusedEnrichment, enrichmentReused: true });
        return;
      }
      // 캐시 파일이 깨졌으면 조용히 넘어가지 않고 아래에서 정상 호출한다
    }

    try {
      llmCalls += 1;
      const enrichment = await enricher.enrich(doc, signal);
      complete(index, { ...doc, enrichment, enrichmentReused: false });
    } catch (err) {
      // 취소는 보류가 아니다 — 위로 그대로 올려 빌드를 멈춘다.
      if (isCancellation(err)) throw err;

      // 스펙 v1.4 §5 — 부분 실패 시 **해당 문서만** 보류하고 빌드는 계속한다.
      const reason = reasonOf(err);
      const record: DeferredDoc = { path: doc.virtualPath, reason };
      deferred.push(record);
      options.onDeferred?.(record);
      complete(index, {
        ...doc,
        enrichment: deferredEnrichment(doc, reason),
        enrichmentReused: false,
        deferredReason: reason,
      });
    }
  });

  return { enriched, deferred, llmCalls, reusedCount };
}

/**
 * 보류된 문서도 위키에는 들어간다 — 본문(마크다운)은 이미 있으니 검색은 되어야 한다.
 * 다만 enrichment 는 비어 있고, 색인에 `deferred: true` 로 남아 다음 빌드에서 재시도된다.
 */
export function deferredEnrichment(doc: ConvertedDoc, reason: string): Enrichment {
  return {
    summary: `이 문서는 아직 분석되지 않았습니다 (${reason}). 다음 빌드에서 다시 시도합니다.`,
    keywords: [],
    sections: extractSections(doc.markdown),
    aliases: [],
    model: 'deferred',
  };
}

function reasonOf(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * 정해진 개수만큼만 동시에 돌리는 워커 풀 (스펙 v1.4 §5 — LLM 호출 동시성 제한).
 * 워커가 던지면 전체가 거부되고, 나머지 워커도 취소 신호를 보고 곧 멈춘다.
 */
export async function mapConcurrent<T>(
  items: T[],
  limit: number,
  worker: (item: T, index: number) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const index = cursor;
      cursor += 1;
      if (index >= items.length) return;
      await worker(items[index]!, index);
    }
  });
  await Promise.all(runners);
}

/* ------------------------------------------------------------------ 목 보조 */

function extractSections(markdown: string): string[] {
  const out: string[] = [];
  for (const line of markdown.split('\n')) {
    const m = /^#{2,3}\s+(.+)$/.exec(line);
    if (m?.[1]) out.push(m[1].trim());
    if (out.length >= 12) break;
  }
  return out;
}

function extractKeywords(markdown: string): string[] {
  const counts = new Map<string, number>();
  for (const raw of markdown.split(/[^\p{L}\p{N}]+/u)) {
    const word = raw.trim();
    if (word.length < 2 || word.length > 20) continue;
    counts.set(word, (counts.get(word) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([word]) => word);
}

export interface CreateEnricherOptions {
  keys: LlmKeyResolver;
  mock?: boolean;
  mockDelayMs?: number;
}

export function createEnricher(options: CreateEnricherOptions): Enricher {
  return options.mock
    ? new MockEnricher(options.mockDelayMs ?? 180)
    : new LlmEnricher({ keys: options.keys });
}
