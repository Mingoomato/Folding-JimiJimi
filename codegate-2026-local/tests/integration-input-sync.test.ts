import fs from 'node:fs/promises';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import type { FileRow } from '@main/db/store';
import { syncNormalizedInputs } from '@main/sidecar/input-sync';
import { sha256File } from '@main/util/fsx';

const enabled = process.env.CODEGATE_INPUT_SYNC_INTEGRATION === '1';

describe.skipIf(!enabled)('production input sync integration', () => {
  it('실제 doc2md 응답을 app-owned LLMWIKI input으로 만든다', async () => {
    const sourcePath = requiredEnv('CODEGATE_INPUT_SYNC_SOURCE');
    const inputRoot = requiredEnv('CODEGATE_INPUT_SYNC_TARGET');
    const relativePath = requiredEnv('CODEGATE_INPUT_SYNC_RELATIVE_PATH');
    const sourceSha256 = await sha256File(sourcePath);
    const stat = await fs.stat(sourcePath);
    const row: FileRow = {
      rootId: 'integration-root',
      relPath: relativePath,
      absPath: sourcePath,
      sha256: sourceSha256,
      size: stat.size,
      mtime: stat.mtime.toISOString(),
      status: 'pending',
      dirty: true,
      deleted: false,
    };

    await expect(
      syncNormalizedInputs({
        files: [row],
        inputRoot,
        doc2mdUrl: requiredEnv('CODEGATE_DOC2MD_URL'),
      }),
    ).resolves.toEqual({ converted: 1, unchanged: 0, removed: 0 });

    const files = (await fs.readdir(inputRoot)).filter((name) => name.endsWith('.md'));
    expect(files).toHaveLength(1);
    const output = await fs.readFile(path.join(inputRoot, files[0]!), 'utf8');
    expect(output).toContain('"schema_version": "1.0.0"');
    expect(output).toContain(`"sha256": "${sourceSha256}"`);
    expect(output).toContain('## 본문');
  });
});

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}
