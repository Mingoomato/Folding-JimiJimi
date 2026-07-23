import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readWikiGraph } from '@main/build/graph';

/**
 * 지식 그래프 리더 — LLMWIKI 빌드 산출물을 읽어 노드·엣지로 만든다.
 *
 * 빌드는 불변이라 우리가 고칠 수 없다. 그래서 **깨진 입력에도 그릴 수 있는 만큼 그리고,
 * 없는 것을 지어내지 않는지**가 검증의 초점이다.
 */
describe('readWikiGraph', () => {
  let storageRoot: string;
  let wikiRoot: string;

  beforeEach(async () => {
    storageRoot = await mkdtemp(path.join(tmpdir(), 'graph-'));
    wikiRoot = path.join(storageRoot, 'tenants', 'local', 'wikis', 'workspace');
    await mkdir(wikiRoot, { recursive: true });
  });

  afterEach(async () => {
    await rm(storageRoot, { recursive: true, force: true });
  });

  const location = () => ({ storageRoot, tenantId: 'local', wikiId: 'workspace' });

  async function seed(
    buildId: string,
    manifest: unknown[],
    links: unknown[],
    { writeCurrent = true } = {},
  ) {
    const buildDir = path.join(wikiRoot, 'builds', buildId);
    await mkdir(path.join(buildDir, 'retrieval'), { recursive: true });
    await writeFile(
      path.join(buildDir, 'manifest.jsonl'),
      manifest.map((m) => JSON.stringify(m)).join('\n'),
    );
    await writeFile(
      path.join(buildDir, 'retrieval', 'links.jsonl'),
      links.map((l) => JSON.stringify(l)).join('\n'),
    );
    if (writeCurrent) {
      await writeFile(path.join(wikiRoot, 'current.json'), JSON.stringify({ build_id: buildId }));
    }
  }

  const doc = (id: string, extra: Record<string, unknown> = {}) => ({
    doc_id: id,
    title: `${id} 문서`,
    doc_type: 'regulation',
    status: 'active',
    source: { uri: `source://docs/${id}.md`, filename: `${id}.md` },
    ...extra,
  });

  it('빌드가 없으면 빈 그래프 — 없는 것을 그리지 않는다', async () => {
    expect(await readWikiGraph(location())).toEqual({ buildId: null, nodes: [], edges: [] });
  });

  it('current.json 이 가리키는 빌드의 문서와 링크를 읽는다', async () => {
    await seed(
      'build-abc',
      [doc('REG-000001'), doc('REG-000002')],
      [{ from_doc_id: 'REG-000001', to_doc_id: 'REG-000002', relation_type: 'supersedes' }],
    );

    const graph = await readWikiGraph(location());

    expect(graph.buildId).toBe('build-abc');
    expect(graph.nodes.map((n) => n.docId)).toEqual(['REG-000001', 'REG-000002']);
    expect(graph.nodes[0]?.sourceUri).toBe('source://docs/REG-000001.md');
    expect(graph.edges).toEqual([
      { from: 'REG-000001', to: 'REG-000002', relationType: 'supersedes' },
    ]);
  });

  it('agent 호환 링크 스키마도 그래프 엣지로 읽는다', async () => {
    await seed(
      'build-agent-compatible',
      [doc('REG-000001'), doc('REP-000001')],
      [
        {
          source_id: 'REG-000001',
          target_id: 'REP-000001',
          relation: 'VERIFIED_BY',
          status: 'VERIFIED',
          evidence_chunk_ids: ['REG-000001@1#sec-001'],
        },
      ],
    );

    expect((await readWikiGraph(location())).edges).toEqual([
      { from: 'REG-000001', to: 'REP-000001', relationType: 'VERIFIED_BY' },
    ]);
  });

  it('한쪽 끝이 없는 링크는 버린다 — 정체불명의 점을 만들지 않는다', async () => {
    await seed(
      'build-abc',
      [doc('REG-000001')],
      [
        { from_doc_id: 'REG-000001', to_doc_id: 'GHOST-000009', relation_type: 'references' },
        { from_doc_id: 'GHOST-000009', to_doc_id: 'REG-000001', relation_type: 'references' },
      ],
    );

    expect((await readWikiGraph(location())).edges).toHaveLength(0);
  });

  it('같은 관계가 섹션마다 반복돼도 문서 단위로 한 번만 남긴다', async () => {
    await seed(
      'build-abc',
      [doc('A-001'), doc('B-001')],
      [
        { from_doc_id: 'A-001', to_doc_id: 'B-001', relation_type: 'references', from_section_id: 'sec-0000000001' },
        { from_doc_id: 'A-001', to_doc_id: 'B-001', relation_type: 'references', from_section_id: 'sec-0000000002' },
        // 관계 종류가 다르면 별개다
        { from_doc_id: 'A-001', to_doc_id: 'B-001', relation_type: 'supersedes' },
      ],
    );

    const edges = (await readWikiGraph(location())).edges;
    expect(edges).toHaveLength(2);
    expect(edges.map((e) => e.relationType).sort()).toEqual(['references', 'supersedes']);
  });

  it('자기 자신을 가리키는 링크는 버린다', async () => {
    await seed('build-abc', [doc('A-001')], [
      { from_doc_id: 'A-001', to_doc_id: 'A-001', relation_type: 'references' },
    ]);
    expect((await readWikiGraph(location())).edges).toHaveLength(0);
  });

  it('깨진 JSONL 한 줄 때문에 나머지를 잃지 않는다', async () => {
    const buildDir = path.join(wikiRoot, 'builds', 'build-abc');
    await mkdir(path.join(buildDir, 'retrieval'), { recursive: true });
    await writeFile(
      path.join(buildDir, 'manifest.jsonl'),
      [JSON.stringify(doc('A-001')), '{ 깨진 줄', JSON.stringify(doc('B-001')), ''].join('\n'),
    );
    await writeFile(path.join(buildDir, 'retrieval', 'links.jsonl'), '');
    await writeFile(path.join(wikiRoot, 'current.json'), JSON.stringify({ build_id: 'build-abc' }));

    expect((await readWikiGraph(location())).nodes.map((n) => n.docId)).toEqual(['A-001', 'B-001']);
  });

  it('links.jsonl 이 아예 없어도 노드는 읽는다 (링크 0개는 오류가 아니다)', async () => {
    const buildDir = path.join(wikiRoot, 'builds', 'build-abc');
    await mkdir(buildDir, { recursive: true });
    await writeFile(path.join(buildDir, 'manifest.jsonl'), JSON.stringify(doc('A-001')));
    await writeFile(path.join(wikiRoot, 'current.json'), JSON.stringify({ build_id: 'build-abc' }));

    const graph = await readWikiGraph(location());
    expect(graph.nodes).toHaveLength(1);
    expect(graph.edges).toEqual([]);
  });

  it('current.json 이 없으면 빌드 폴더가 있어도 읽지 않는다 — 활성 빌드만 본다', async () => {
    await seed('build-abc', [doc('A-001')], [], { writeCurrent: false });
    expect(await readWikiGraph(location())).toEqual({ buildId: null, nodes: [], edges: [] });
  });
});
