/**
 * ① 변환 단계 (스펙 v1.4 §1 L2 · §2 "변환 모듈(kordoc 어댑터 → INPUT_CONTRACT md) npm 패키지 — 민규").
 *
 * 변환 모듈은 아직 배포 전이다. 그래서 경계를 인터페이스로 끊어 두고 구현을 둘로 나눈다.
 *   · `RealConverter` — 실물 패키지를 동적 import. **없으면 한국어로 크게 실패한다**
 *                       (조용히 목으로 갈아타지 않는다 — 스펙 v1.4 §5 silent fail 금지)
 *   · `MockConverter` — `CODEGATE_MOCK=1` 전용. L3 kordoc 목(픽스처)이 읽어 준 마크다운에
 *                       INPUT_CONTRACT 머리말만 붙인다
 *
 * 파싱 자체는 두 구현 모두 **L3 kordoc 래퍼를 그대로 재사용한다** — 변환 모듈의 정의가
 * "kordoc 어댑터"이므로 문서를 마크다운으로 여는 일은 이미 L3 가 하고 있다.
 */
import type { KordocApi } from '@contracts';
import { UserFacingError } from '@main/util/errors';
import type { PreviousBuild } from './cache';
import {
  isCancellation,
  makeDocId,
  titleFromMarkdown,
  throwIfCancelled,
  type ConvertedDoc,
  type DeferredDoc,
  type SourceDoc,
} from './types';

/** 변환 모듈 npm 패키지 이름 (환경변수로 바꿔 끼울 수 있다). */
const CONVERT_PACKAGE = process.env.CODEGATE_CONVERT_MODULE ?? '@codegate/convert';

const CONVERT_MODULE_MISSING =
  '문서 변환 모듈을 찾을 수 없습니다. 변환 모듈이 아직 설치되지 않았습니다 — ' +
  '개발 중이라면 CODEGATE_MOCK=1 로 목 파이프라인을 사용해 주세요.';

/** 변환 모듈이 노출해야 할 최소 계약 (M0 인터페이스 — 우창 ↔ 민규). */
export interface ConvertModule {
  /** 원본에서 뽑은 마크다운을 INPUT_CONTRACT 형식으로 정규화해 돌려준다. */
  toInputContract(input: {
    markdown: string;
    /** 원본의 가상 경로 (`<rootId>/<상대경로>`) */
    sourcePath: string;
    docId: string;
    title: string;
  }): string | Promise<string>;
}

export interface ConvertRequest {
  doc: SourceDoc;
  /** 이전 빌드에서 물려받은 리비전 (내용이 바뀌었으므로 +1 된 값이 들어온다) */
  rev: number;
  signal?: AbortSignal;
}

/** ① 단계의 경계. 실물이 오면 `RealConverter` 만 갈아 끼우면 된다. */
export interface DocumentConverter {
  convert(request: ConvertRequest): Promise<ConvertedDoc>;
}

let modulePromise: Promise<ConvertModule> | null = null;

/** 변환 모듈 동적 로드 — 한 번만 시도하고 결과를 재사용한다. */
export async function loadConvertModule(): Promise<ConvertModule> {
  modulePromise ??= (async () => {
    let mod: { toInputContract?: unknown; default?: { toInputContract?: unknown } };
    try {
      mod = (await import(/* @vite-ignore */ CONVERT_PACKAGE)) as typeof mod;
    } catch (err) {
      throw new UserFacingError(CONVERT_MODULE_MISSING, { cause: err });
    }
    const fn = mod.toInputContract ?? mod.default?.toInputContract;
    if (typeof fn !== 'function') {
      throw new UserFacingError(
        `${CONVERT_PACKAGE} 에서 toInputContract 를 찾지 못했습니다. 변환 모듈 버전을 확인해 주세요.`,
      );
    }
    return { toInputContract: fn as ConvertModule['toInputContract'] };
  })();

  try {
    return await modulePromise;
  } catch (err) {
    modulePromise = null; // 다음 빌드에서 다시 시도할 수 있게 캐시를 비운다
    throw err;
  }
}

/** 실물 변환 모듈 + L3 kordoc 파싱. */
export class RealConverter implements DocumentConverter {
  constructor(private readonly kordoc: KordocApi) {}

  async convert({ doc, rev, signal }: ConvertRequest): Promise<ConvertedDoc> {
    throwIfCancelled(signal);
    // 변환 모듈이 없으면 여기서 크게 실패한다 — 빈 위키를 만들어 놓고 성공한 척하지 않는다.
    const mod = await loadConvertModule();

    // 문서를 마크다운으로 여는 일은 L3 가 이미 한다 (스펙 v1.4 §2 "kordoc 어댑터").
    const raw = await this.kordoc.parse(doc.absPath);
    const docId = makeDocId(doc.virtualPath);
    const title = titleFromMarkdown(raw, doc.virtualPath);
    const markdown = await mod.toInputContract({
      markdown: raw,
      sourcePath: doc.virtualPath,
      docId,
      title,
    });

    return {
      virtualPath: doc.virtualPath,
      docId,
      title,
      markdown,
      sourceSha256: doc.sha256,
      rev,
      reused: false,
    };
  }
}

/**
 * 픽스처 기반 목 (`CODEGATE_MOCK=1`).
 * `MockKordoc` 이 `<fixtureDir>/<파일명>.md` 를 읽어 주므로 픽스처만 갈아 끼우면
 * 실물 문서 없이도 파이프라인 전체가 돈다.
 */
export class MockConverter implements DocumentConverter {
  constructor(
    private readonly kordoc: KordocApi,
    /** 진행률이 눈에 보이도록 넣는 인위적 지연 */
    private readonly delayMs = 0,
  ) {}

  async convert({ doc, rev, signal }: ConvertRequest): Promise<ConvertedDoc> {
    throwIfCancelled(signal);
    const raw = await this.kordoc.parse(doc.absPath);
    if (this.delayMs > 0) await new Promise((r) => setTimeout(r, this.delayMs));

    const docId = makeDocId(doc.virtualPath);
    const title = titleFromMarkdown(raw, doc.virtualPath);
    return {
      virtualPath: doc.virtualPath,
      docId,
      title,
      markdown: withInputContractHeader(raw, { docId, title, sourcePath: doc.virtualPath }),
      sourceSha256: doc.sha256,
      rev,
      reused: false,
    };
  }
}

/** INPUT_CONTRACT 머리말 — 실물 모듈이 붙일 최소 메타데이터를 목에서도 흉내 낸다. */
function withInputContractHeader(
  markdown: string,
  meta: { docId: string; title: string; sourcePath: string },
): string {
  const header = [
    '---',
    `doc_id: ${meta.docId}`,
    `title: ${meta.title}`,
    `source_path: ${meta.sourcePath}`,
    '---',
    '',
  ].join('\n');
  return `${header}${markdown.trimStart()}\n`;
}

export interface CreateConverterOptions {
  kordoc: KordocApi;
  mock?: boolean;
  mockDelayMs?: number;
}

export function createConverter(options: CreateConverterOptions): DocumentConverter {
  return options.mock
    ? new MockConverter(options.kordoc, options.mockDelayMs ?? 40)
    : new RealConverter(options.kordoc);
}

/* ============================================================================
 * 오케스트레이션 — 캐시 재사용 · 진행 보고 · 취소
 * ========================================================================== */

export interface RunConversionOptions {
  docs: SourceDoc[];
  converter: DocumentConverter;
  /** 직전 승격 빌드 — 해시가 같은 문서는 변환도 건너뛴다 */
  previous: PreviousBuild;
  signal?: AbortSignal;
  onProgress?: (done: number, total: number) => void;
  onDeferred?: (deferred: DeferredDoc) => void;
}

export interface RunConversionResult {
  converted: ConvertedDoc[];
  deferred: DeferredDoc[];
  /** 변환 모듈을 실제로 부른 문서 수 */
  convertedCount: number;
  reusedCount: number;
}

/**
 * 변환을 순서대로 돌린다 (로컬 CPU·서브프로세스 작업이라 동시성보다 예측 가능성이 낫다).
 *
 * - 해시가 같으면 이전 빌드의 마크다운을 그대로 쓴다
 * - 한 문서를 못 읽어도 빌드 전체를 죽이지 않는다 — 그 문서만 보류한다 (스펙 v1.4 §5)
 */
export async function runConversion(options: RunConversionOptions): Promise<RunConversionResult> {
  const { docs, converter, previous, signal } = options;
  const converted: ConvertedDoc[] = [];
  const deferred: DeferredDoc[] = [];
  let convertedCount = 0;
  let reusedCount = 0;

  for (const [index, doc] of docs.entries()) {
    throwIfCancelled(signal);
    const previousEntry = previous.entryFor(doc.virtualPath);
    const cached = previous.reusable(doc.virtualPath, doc.sha256);

    try {
      if (cached) {
        const markdown = await previous.readMarkdown(cached);
        if (markdown !== null) {
          reusedCount += 1;
          converted.push({
            virtualPath: doc.virtualPath,
            docId: cached.docId,
            title: cached.title,
            markdown,
            sourceSha256: doc.sha256,
            // 내용이 그대로면 리비전도 그대로다 — 인용의 rev 가 헛되이 늘지 않는다
            rev: cached.rev,
            reused: true,
          });
          options.onProgress?.(index + 1, docs.length);
          continue;
        }
      }
      convertedCount += 1;
      converted.push(
        await converter.convert({ doc, rev: (previousEntry?.rev ?? 0) + 1, signal }),
      );
    } catch (err) {
      if (isCancellation(err)) throw err;
      const record: DeferredDoc = { path: doc.virtualPath, reason: reasonOf(err) };
      deferred.push(record);
      options.onDeferred?.(record);
    }
    options.onProgress?.(index + 1, docs.length);
  }

  return { converted, deferred, convertedCount, reusedCount };
}

function reasonOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
