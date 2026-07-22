#!/usr/bin/env node
import { randomBytes } from 'node:crypto';
import { chmod, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import {
  agentDir,
  cloudApiDir,
  convertDir,
  ensureNode22,
  executable,
  localDir,
  localRuntimeDir,
  rootDir,
  run,
  wikiBuilderDir,
} from './lib.mjs';

ensureNode22();
const pnpm = executable('pnpm');
const uv = executable('uv');
const skipDoc2md = process.argv.includes('--skip-doc2md');
const allowRecentPackages = process.argv.includes('--allow-recent-packages');

const envFile = path.join(rootDir, '.env');
try {
  let envText = await readFile(envFile, 'utf8');
  if (/^CODEGATE_SESSION_PEPPER=(?:replace-with.*)?$/m.test(envText)) {
    envText = envText.replace(
      /^CODEGATE_SESSION_PEPPER=.*$/m,
      `CODEGATE_SESSION_PEPPER=${randomBytes(32).toString('hex')}`,
    );
    await writeFile(envFile, envText, { encoding: 'utf8', mode: 0o600 });
  }
  await chmod(envFile, 0o600);
} catch (error) {
  if (error?.code !== 'ENOENT') throw error;
  const example = await readFile(path.join(rootDir, '.env.example'), 'utf8');
  const generated = example.replace(
    /^CODEGATE_SESSION_PEPPER=.*$/m,
    `CODEGATE_SESSION_PEPPER=${randomBytes(32).toString('hex')}`,
  );
  await writeFile(envFile, generated, { encoding: 'utf8', mode: 0o600 });
  console.log('[환경] .env와 로컬 session pepper를 생성했습니다.');
}

await run(
  pnpm,
  [
    'install',
    '--frozen-lockfile',
    ...(allowRecentPackages ? ['--config.minimum-release-age=0'] : []),
  ],
  { cwd: localDir, name: 'Electron 의존성' },
);
await run(uv, ['sync', '--project', agentDir, '--locked', '--python', '3.12'], {
  cwd: rootDir,
  name: 'Agent sidecar',
});
await run(uv, ['sync', '--project', cloudApiDir, '--locked', '--python', '3.12', '--extra', 'dev'], {
  cwd: rootDir,
  name: 'Google OAuth cloud API',
});
await run(
  uv,
  ['sync', '--project', wikiBuilderDir, '--locked', '--python', '3.12', '--extra', 'dev'],
  { cwd: rootDir, name: 'LLMWIKI builder' },
);
await run(
  uv,
  ['sync', '--project', localRuntimeDir, '--locked', '--python', '3.12', '--extra', 'dev'],
  { cwd: rootDir, name: 'Legacy local runtime contract tests' },
);

if (skipDoc2md) {
  console.log('\n[doc2md] --skip-doc2md로 건너뛰었습니다. 실제 앱 실행 전에는 다시 setup하세요.');
} else {
  await run(
    uv,
    ['sync', '--project', convertDir, '--locked', '--python', '3.12', '--extra', 'test'],
    { cwd: rootDir, name: 'doc2md (OCR 패키지 포함)' },
  );
}

console.log('\n설치가 끝났습니다. .env의 세 값을 채운 뒤 `pnpm doctor`, `pnpm dev`를 실행하세요.');
