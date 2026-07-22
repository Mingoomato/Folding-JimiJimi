import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FileRow } from '@main/db/store';
import {
  canonicalSourceUri,
  localDoc2MdBaseUrl,
  stableDocumentId,
  syncNormalizedInputs,
} from '@main/sidecar/input-sync';

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => fs.rm(root, { recursive: true, force: true })));
});

describe('sidecar normalized input sync', () => {
  it('원본 절대경로를 받는 doc2md는 loopback HTTP로 제한한다', () => {
    expect(localDoc2MdBaseUrl('http://127.0.0.1:8123/')).toBe('http://127.0.0.1:8123');
    expect(localDoc2MdBaseUrl('http://[::1]:8123')).toBe('http://[::1]:8123');
    expect(() => localDoc2MdBaseUrl('https://doc2md.example.com')).toThrow('loopback');
    expect(() => localDoc2MdBaseUrl('http://user:secret@127.0.0.1:8123')).toThrow('loopback');
  });

  it('예약문자와 Unicode를 segment 단위로 encoding한 canonical source URI를 만든다', () => {
    expect(canonicalSourceUri('규정/100% #1?.md')).toBe(
      'source://%EA%B7%9C%EC%A0%95/100%25%20%231%3F.md',
    );
    const decomposed = '한글'.normalize('NFD');
    expect(decodeURIComponent(canonicalSourceUri(`${decomposed}.md`).slice('source://'.length))).toBe(
      `${decomposed}.md`,
    );
  });

  it('새 원본을 doc2md로 변환해 LLMWIKI 계약에 맞는 입력을 만든다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, '규정/보관.md', 'a'.repeat(64));
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          body: '# 보관 규정\n\n개인정보는 1년간 보관한다.',
          source_sha256: row.sha256,
          frontmatter: { title: '보관 규정', language: 'ko' },
        }),
        { status: 200 },
      ),
    );
    const progress: Array<{ done: number; total: number; relativePath?: string; result?: string }> = [];

    await expect(
      syncNormalizedInputs({
        files: [row],
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123/',
        fetchImpl: fetchImpl as typeof fetch,
        onProgress: (event) => progress.push(event),
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 0 });
    expect(progress).toEqual([
      { done: 0, total: 1 },
      { done: 0, total: 1, relativePath: row.relPath },
      { done: 1, total: 1, relativePath: row.relPath, result: 'converted' },
    ]);

    const id = stableDocumentId(row.relPath);
    const output = await fs.readFile(path.join(inputRoot, `${id}.md`), 'utf8');
    expect(output).toContain(`"id": "${id}"`);
    expect(output).toContain('"schema_version": "1.0.0"');
    expect(output).toContain(
      '"uri": "source://%EA%B7%9C%EC%A0%95/%EB%B3%B4%EA%B4%80.md"',
    );
    expect(output).toContain('# 보관 규정\n\n## 본문');
    expect(output).not.toContain('chunk_no');
    expect(fetchImpl).toHaveBeenCalledWith(
      'http://127.0.0.1:8123/v2/convert',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('source URI와 SHA가 같으면 승인된 normalized input을 덮어쓰지 않는다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'manual.md', 'b'.repeat(64));
    const firstFetch = conversionFetch(row.sha256);
    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: firstFetch,
    });
    const target = path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`);
    const approved = `${await fs.readFile(target, 'utf8')}\n승인 후 보존할 내용\n`;
    await fs.writeFile(target, approved);
    const secondFetch = vi.fn();

    await expect(
      syncNormalizedInputs({ files: [row], inputRoot, fetchImpl: secondFetch as typeof fetch }),
    ).resolves.toEqual({ converted: 0, unchanged: 1, removed: 0 });
    expect(await fs.readFile(target, 'utf8')).toBe(approved);
    expect(secondFetch).not.toHaveBeenCalled();
  });

  it('원본 SHA가 바뀌면 revision을 올리고 ownership manifest의 stale 입력만 제거한다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const initial = fileRow(root, 'policy.txt', 'c'.repeat(64));
    const stale = fileRow(root, 'stale.txt', 'e'.repeat(64));
    await syncNormalizedInputs({
      files: [initial, stale],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: vi.fn(async (_url, init) => {
        const request = JSON.parse(String(init?.body)) as { expected_source_sha256: string };
        return conversionResponse(request.expected_source_sha256);
      }) as typeof fetch,
    });
    await fs.writeFile(path.join(inputRoot, 'MANUAL.md'), 'manual');
    const changed = { ...initial, sha256: 'd'.repeat(64) };

    await expect(
      syncNormalizedInputs({
        files: [changed],
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123',
        fetchImpl: conversionFetch(changed.sha256),
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 1 });

    const output = await fs.readFile(
      path.join(inputRoot, `${stableDocumentId(changed.relPath)}.md`),
      'utf8',
    );
    expect(output).toContain('"revision": "2"');
    await expect(
      fs.stat(path.join(inputRoot, `${stableDocumentId(stale.relPath)}.md`)),
    ).rejects.toThrow();
    await expect(fs.stat(path.join(inputRoot, 'MANUAL.md'))).resolves.toBeDefined();
  });

  it('동일한 원본의 rename은 document ID를 유지하고 revision 계보를 잇는다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const initial = fileRow(root, 'before.md', 'f'.repeat(64));
    await syncNormalizedInputs({
      files: [initial],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: conversionFetch(initial.sha256),
    });
    const originalId = stableDocumentId(initial.relPath);
    const renamed = { ...initial, relPath: 'after #1.md', absPath: path.join(root, 'after #1.md') };

    await syncNormalizedInputs({
      files: [renamed],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: conversionFetch(renamed.sha256),
    });

    const output = await fs.readFile(path.join(inputRoot, `${originalId}.md`), 'utf8');
    expect(output).toContain(`"id": "${originalId}"`);
    expect(output).toContain('"revision": "2"');
    expect(output).toContain('"uri": "source://after%20%231.md"');
    expect(stableDocumentId(renamed.relPath)).not.toBe(originalId);
  });

  it('중복 H2를 고유 heading으로 정규화하고 기존 invalid input도 같은 SHA에서 복구한다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'duplicate-headings.md', '9'.repeat(64));
    const duplicateFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          body: '# 문서\n\n## 반복\n\n첫 번째\n\n## 반복\n\n두 번째',
          source_sha256: row.sha256,
          frontmatter: { title: '문서', language: 'ko' },
        }),
        { status: 200 },
      ),
    ) as typeof fetch;

    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: duplicateFetch,
    });
    const target = path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`);
    const normalized = await fs.readFile(target, 'utf8');
    expect(normalized).toContain('\n## 반복\n');
    expect(normalized).toContain('\n## 반복 · 2\n');

    // 이전 adapter가 남긴 duplicate anchor 입력도 source SHA가 같다는 이유로 재사용하지 않는다.
    await fs.writeFile(target, normalized.replace('## 반복 · 2', '## 반복'));
    await expect(
      syncNormalizedInputs({
        files: [row],
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123',
        fetchImpl: duplicateFetch,
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 0 });
    const repaired = await fs.readFile(target, 'utf8');
    expect(repaired).toContain('"revision": "2"');
    expect(repaired).toContain('\n## 반복 · 2\n');
  });

  it.each([
    ['H1 누락', (body: string) => body.replace('# 문서\n\n', '')],
    ['H1 불일치', (body: string) => body.replace('# 문서', '# 다른 제목')],
    ['H2 누락', (body: string) => body.replace('## 내용\n\n', '')],
    ['빈 H2', (body: string) => body.replace('## 내용\n\n본문', '## 내용\n')],
  ])('같은 SHA여도 구조가 손상된 input을 다시 변환한다: %s', async (_label, corrupt) => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'invalid-structure.md', '6'.repeat(64));
    const fetchImpl = conversionFetch(row.sha256);
    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl,
    });
    const target = path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`);
    await fs.writeFile(target, corrupt(await fs.readFile(target, 'utf8')));

    await expect(
      syncNormalizedInputs({
        files: [row],
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123',
        fetchImpl,
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 0 });
    expect(await fs.readFile(target, 'utf8')).toContain('"revision": "2"');
  });

  it('긴 H2 본문을 overlap 없는 bounded section으로 나눈다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'large-section.md', '8'.repeat(64));
    const longBody = ['가'.repeat(1_100), '나'.repeat(1_100), '다'.repeat(1_100)].join('\n\n');

    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: vi.fn(async () =>
        new Response(
          JSON.stringify({
            body: `# 문서\n\n## 본문\n\n${longBody}`,
            source_sha256: row.sha256,
            frontmatter: { title: '문서', language: 'ko' },
          }),
          { status: 200 },
        ),
      ) as typeof fetch,
    });

    const output = await fs.readFile(
      path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`),
      'utf8',
    );
    expect(output).toContain('\n## 본문\n');
    expect(output).toContain('\n## 본문 · 2\n');
    expect(output).not.toContain('\n## 본문 · 4\n');
    for (const section of output.split(/^## /m).slice(1)) {
      expect(section.replace(/^.*\n/, '').trim().length).toBeLessThanOrEqual(2_200);
    }
  });

  it('긴 fenced code block을 균형 잡힌 bounded section으로 나누고 같은 SHA 손상도 복구한다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'large-code.md', '5'.repeat(64));
    const originalBody = `# 문서\n\n## 코드\n\n\`\`\`python\n${'x'.repeat(5_000)}\n\`\`\``;
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          body: originalBody,
          source_sha256: row.sha256,
          frontmatter: { title: '문서', language: 'ko' },
        }),
        { status: 200 },
      ),
    ) as typeof fetch;

    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl,
    });
    const target = path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`);
    let output = await fs.readFile(target, 'utf8');
    assertBoundedFencedSections(output);

    const frontmatter = output.match(/^---\s*\n[\s\S]*?\n---\s*\n/)?.[0];
    expect(frontmatter).toBeDefined();
    await fs.writeFile(target, `${frontmatter}${originalBody}\n`);
    await expect(
      syncNormalizedInputs({
        files: [row],
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123',
        fetchImpl,
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 0 });
    output = await fs.readFile(target, 'utf8');
    expect(output).toContain('"revision": "2"');
    assertBoundedFencedSections(output);
  });

  it('H1과 첫 H2 사이의 긴 preamble도 첫 section 분할에 포함한다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const row = fileRow(root, 'large-preamble.md', '7'.repeat(64));
    const preamble = ['가'.repeat(1_100), '나'.repeat(1_100), '다'.repeat(1_100)].join('\n\n');

    await syncNormalizedInputs({
      files: [row],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: vi.fn(async () =>
        new Response(
          JSON.stringify({
            body: `# 문서\n\n${preamble}\n\n## 세부\n\n짧은 본문`,
            source_sha256: row.sha256,
            frontmatter: { title: '문서', language: 'ko' },
          }),
          { status: 200 },
        ),
      ) as typeof fetch,
    });

    const output = await fs.readFile(
      path.join(inputRoot, `${stableDocumentId(row.relPath)}.md`),
      'utf8',
    );
    const markdown = output.replace(/^---\s*\n[\s\S]*?\n---\s*\n/, '');
    expect(markdown).toMatch(/^# 문서\n## 세부\n/m);
    expect(markdown).toContain('\n## 세부 · 2\n');
    for (const section of markdown.split(/^## /m).slice(1)) {
      expect(section.replace(/^.*\n/, '').trim().length).toBeLessThanOrEqual(2_200);
    }
  });

  it('marker 없는 non-empty input 디렉터리는 수정하지 않는다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    await fs.mkdir(inputRoot);
    const manual = path.join(inputRoot, 'DOC-FFFFFFFFFFFFFFFF.md');
    await fs.writeFile(manual, 'do not touch');

    await expect(syncNormalizedInputs({ files: [], inputRoot })).rejects.toThrow('빈 디렉터리');
    await expect(fs.readFile(manual, 'utf8')).resolves.toBe('do not touch');
  });

  it('동일 SHA의 rename/copy가 모호하면 각각 새 ID로 수렴하고 중간 실패하지 않는다', async () => {
    const root = await temporaryRoot();
    const inputRoot = path.join(root, 'source-md');
    const original = fileRow(root, 'original.md', '1'.repeat(64));
    await syncNormalizedInputs({
      files: [original],
      inputRoot,
      doc2mdUrl: 'http://127.0.0.1:8123',
      fetchImpl: conversionFetch(original.sha256),
    });
    const copies = ['copy-a.md', 'copy-b.md'].map((relPath) => ({
      ...original,
      relPath,
      absPath: path.join(root, relPath),
    }));

    await expect(
      syncNormalizedInputs({
        files: copies,
        inputRoot,
        doc2mdUrl: 'http://127.0.0.1:8123',
        fetchImpl: conversionFetch(original.sha256),
      }),
    ).resolves.toEqual({ converted: 2, unchanged: 0, removed: 1 });
    const markdown = (await fs.readdir(inputRoot)).filter((name) => name.endsWith('.md'));
    expect(markdown.sort()).toEqual(
      copies.map((copy) => `${stableDocumentId(copy.relPath)}.md`).sort(),
    );
  });
});

async function temporaryRoot(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-input-sync-'));
  temporaryRoots.push(root);
  return root;
}

function fileRow(root: string, relPath: string, sha256: string): FileRow {
  return {
    rootId: 'root-test',
    relPath,
    absPath: path.join(root, relPath),
    sha256,
    size: 10,
    mtime: new Date(0).toISOString(),
    status: 'pending',
    dirty: true,
    deleted: false,
  };
}

function conversionFetch(sourceSha256: string): typeof fetch {
  return vi.fn(async () => conversionResponse(sourceSha256)) as typeof fetch;
}

function conversionResponse(sourceSha256: string): Response {
  return new Response(
    JSON.stringify({
      body: '# 문서\n\n## 내용\n\n본문',
      source_sha256: sourceSha256,
      frontmatter: { title: '문서', language: 'ko' },
    }),
    { status: 200 },
  );
}

function assertBoundedFencedSections(output: string): void {
  const markdown = output.replace(/^---\s*\n[\s\S]*?\n---\s*\n/, '');
  const sections = markdown.split(/^## /m).slice(1);
  expect(sections.length).toBeGreaterThan(1);
  for (const section of sections) {
    const content = section.replace(/^.*\n/, '').trim();
    expect(content.length).toBeLessThanOrEqual(2_200);
    expect(content.match(/^\s*`{3,}/gm)).toHaveLength(2);
  }
}
