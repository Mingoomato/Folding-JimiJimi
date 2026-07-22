/**
 * 로컬 빌드 파이프라인의 불변식 (스펙 v1.4 §5).
 *
 * v1.3 의 ACK 프로토콜 테스트를 대체한다 — 서버로 산출물을 보내고 되받는 경로가
 * 사라졌으므로, 이제 지켜야 할 것은 셋이다:
 *   ① 조립이 실패해도 이전 빌드와 current.json 은 그대로 남는다
 *   ② 한 문서의 enrichment 실패가 빌드 전체를 죽이지 않는다 (보류)
 *   ③ 내용이 안 바뀐 문서는 LLM 을 다시 부르지 않는다 (비용 통제)
 */
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MockBuilderCore, promoteBuild, type BuilderCore } from '@main/build/assemble';
import { PreviousBuild } from '@main/build/cache';
import { readCurrentPointer } from '@main/build/current';
import { MOCK_ENRICH_FAIL_MARK, MockEnricher, runEnrichment } from '@main/build/enrich';
import type { ConvertedDoc, EnrichedDoc } from '@main/build/types';

let wikiDir: string;

beforeEach(async () => {
  wikiDir = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-wiki-'));
});

afterEach(async () => {
  await fs.rm(wikiDir, { recursive: true, force: true });
});

const sha = (s: string) => createHash('sha256').update(s).digest('hex');

function converted(name: string, body: string, rev = 1): ConvertedDoc {
  return {
    virtualPath: `root1/${name}.md`,
    docId: name.toUpperCase(),
    title: name,
    markdown: `# ${name}\n\n${body}\n`,
    sourceSha256: sha(body),
    rev,
    reused: false,
  };
}

/** enrichment 를 통과시켜 EnrichedDoc 로 만든다 (조립 테스트용 지름길). */
async function enrichAll(docs: ConvertedDoc[]): Promise<EnrichedDoc[]> {
  const result = await runEnrichment({
    docs,
    enricher: new MockEnricher(0),
    previous: PreviousBuild.empty(),
  });
  return result.enriched;
}

describe('① 조립 실패 시 이전 빌드 보존 (스펙 v1.4 §5)', () => {
  it('조립이 도중에 터져도 current.json 과 직전 빌드가 그대로 남는다', async () => {
    const first = await enrichAll([converted('alpha', '첫 번째 내용')]);
    const promoted = await promoteBuild({
      wikiDir,
      buildId: 'build-1',
      builder: new MockBuilderCore(),
      docs: first,
    });

    const pointerBefore = await readCurrentPointer(wikiDir);
    expect(pointerBefore?.buildId).toBe('build-1');
    const firstDocBefore = await fs.readFile(
      path.join(promoted.dir, 'docs', 'ALPHA.md'),
      'utf8',
    );

    // 조립 중간에 실패하는 빌더
    const brokenBuilder: BuilderCore = {
      async assemble() {
        throw new Error('빌더 코어가 조립에 실패했습니다.');
      },
    };

    const second = await enrichAll([converted('beta', '두 번째 내용')]);
    await expect(
      promoteBuild({
        wikiDir,
        buildId: 'build-2',
        builder: brokenBuilder,
        docs: second,
      }),
    ).rejects.toThrow();

    // 포인터는 여전히 build-1
    const pointerAfter = await readCurrentPointer(wikiDir);
    expect(pointerAfter?.buildId).toBe('build-1');

    // 이전 빌드 내용도 손대지 않았다
    await expect(fs.readFile(path.join(promoted.dir, 'docs', 'ALPHA.md'), 'utf8')).resolves.toBe(
      firstDocBefore,
    );

    // 실패한 빌드는 승격 디렉터리를 남기지 않는다
    await expect(fs.access(path.join(wikiDir, 'builds', 'build-2'))).rejects.toThrow();
  });
});

describe('② enrichment 부분 실패는 보류로 처리 (스펙 v1.4 §5)', () => {
  it('한 문서가 실패해도 나머지는 완주하고 그 문서만 보류된다', async () => {
    const docs = [
      converted('good1', '정상 문서'),
      converted('bad', `실패 유도 ${MOCK_ENRICH_FAIL_MARK}`),
      converted('good2', '또 다른 정상 문서'),
    ];

    const result = await runEnrichment({
      docs,
      enricher: new MockEnricher(0),
      previous: PreviousBuild.empty(),
    });

    // 빌드는 계속된다 — 세 문서 모두 결과에 남는다
    expect(result.enriched).toHaveLength(3);
    expect(result.deferred).toHaveLength(1);
    expect(result.deferred[0].path).toBe('root1/bad.md');
    expect(result.deferred[0].reason).toBeTruthy();

    const bad = result.enriched.find((d) => d.virtualPath === 'root1/bad.md');
    expect(bad?.deferredReason).toBeTruthy();

    // 나머지는 정상적으로 enrichment 를 받았다
    const good = result.enriched.find((d) => d.virtualPath === 'root1/good1.md');
    expect(good?.deferredReason).toBeUndefined();
    expect(good?.enrichment.summary).toBeTruthy();

    // 보류된 문서가 있어도 조립·승격은 성공해야 한다
    const promoted = await promoteBuild({
      wikiDir,
      buildId: 'build-1',
      builder: new MockBuilderCore(),
      docs: result.enriched,
    });
    expect((await readCurrentPointer(wikiDir))?.buildId).toBe(promoted.buildId);
  });
});

describe('③ 변경되지 않은 문서는 재-enrichment 하지 않는다 (스펙 v1.4 §5 비용 통제)', () => {
  it('해시가 같으면 LLM 호출이 0회이고 이전 결과를 물려받는다', async () => {
    const docs = [converted('alpha', '내용 A'), converted('beta', '내용 B')];

    const firstRun = await runEnrichment({
      docs,
      enricher: new MockEnricher(0),
      previous: PreviousBuild.empty(),
    });
    expect(firstRun.llmCalls).toBe(2);
    expect(firstRun.reusedCount).toBe(0);

    await promoteBuild({
      wikiDir,
      buildId: 'build-1',
      builder: new MockBuilderCore(),
      docs: firstRun.enriched,
    });

    // 같은 내용으로 다시 — 이전 빌드를 읽어 재사용해야 한다
    const previous = await PreviousBuild.load(wikiDir);
    const secondRun = await runEnrichment({
      docs,
      enricher: new MockEnricher(0),
      previous,
    });

    expect(secondRun.llmCalls).toBe(0);
    expect(secondRun.reusedCount).toBe(2);
    expect(secondRun.enriched.every((d) => d.enrichmentReused)).toBe(true);
  });

  it('내용이 바뀐 문서만 다시 부른다', async () => {
    const before = [converted('alpha', '내용 A'), converted('beta', '내용 B')];
    const firstRun = await runEnrichment({
      docs: before,
      enricher: new MockEnricher(0),
      previous: PreviousBuild.empty(),
    });
    await promoteBuild({
      wikiDir,
      buildId: 'build-1',
      builder: new MockBuilderCore(),
      docs: firstRun.enriched,
    });

    // beta 만 내용이 바뀌었다 → 해시가 달라진다
    const after = [converted('alpha', '내용 A'), converted('beta', '내용 B 수정됨', 2)];
    const secondRun = await runEnrichment({
      docs: after,
      enricher: new MockEnricher(0),
      previous: await PreviousBuild.load(wikiDir),
    });

    expect(secondRun.llmCalls).toBe(1);
    expect(secondRun.reusedCount).toBe(1);
  });
});
