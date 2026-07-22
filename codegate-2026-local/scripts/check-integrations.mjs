#!/usr/bin/env node
import { access } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const projectDir = process.cwd();
const strict = process.argv.includes('--strict');
const executable = process.platform === 'win32' ? 'codegate-local.exe' : 'codegate-local';
const sidecarBin =
  process.env.CODEGATE_SIDECAR_BIN ??
  path.resolve(
    projectDir,
    '..',
    'Backend',
    '.venv',
    process.platform === 'win32' ? 'Scripts' : 'bin',
    executable,
  );
const llmwikiRoot =
  process.env.CODEGATE_LLMWIKI_PROJECT_ROOT ?? path.resolve(projectDir, '..', 'LLMWIKI');

const results = [];

async function exists(candidate) {
  try {
    await access(candidate);
    return true;
  } catch {
    return false;
  }
}

function record(name, status, detail) {
  results.push({ name, status, detail });
}

if (await exists(sidecarBin)) {
  record('성주 sidecar', 'ready', sidecarBin);
} else {
  record('성주 sidecar', 'error', `실행 파일 없음: ${sidecarBin}`);
}

const llmwikiFiles = [
  path.join(llmwikiRoot, 'wiki-builder', 'config', 'wiki.yaml'),
  path.join(llmwikiRoot, 'wiki-builder', 'src', 'wiki_builder'),
];
if ((await Promise.all(llmwikiFiles.map(exists))).every(Boolean)) {
  record('용휘 LLMWIKI bundle', 'ready', llmwikiRoot);
} else {
  record('용휘 LLMWIKI bundle', 'pending', `bundle 경로 확인 필요: ${llmwikiRoot}`);
}

for (const [name, envName, exportName] of [
  ['민규 convert module', 'CODEGATE_CONVERT_MODULE', 'toInputContract'],
  ['용휘 builder module', 'CODEGATE_BUILDER_MODULE', 'assemble'],
]) {
  const moduleName = process.env[envName];
  if (!moduleName) {
    record(name, 'pending', `${envName} 미설정`);
    continue;
  }
  try {
    const imported = await import(moduleName);
    const candidate = imported[exportName] ?? imported.default?.[exportName];
    record(
      name,
      typeof candidate === 'function' ? 'ready' : 'error',
      typeof candidate === 'function' ? moduleName : `${exportName} export 없음`,
    );
  } catch (error) {
    record(name, 'error', `${moduleName}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

const kordocBin = process.env.CODEGATE_KORDOC_BIN ?? 'kordoc';
const kordoc = spawnSync(kordocBin, ['--version'], { encoding: 'utf8' });
if (kordoc.error) {
  record('kordoc', 'pending', `실행 파일을 찾지 못함: ${kordocBin}`);
} else if (kordoc.status === 0) {
  record('kordoc', 'ready', (kordoc.stdout || kordoc.stderr).trim() || kordocBin);
} else {
  record('kordoc', 'error', `--version 종료 코드 ${kordoc.status}`);
}

const doc2mdUrl = process.env.CODEGATE_DOC2MD_URL;
if (!doc2mdUrl) {
  record('민규 doc2md', 'pending', 'CODEGATE_DOC2MD_URL 미설정');
} else {
  try {
    const response = await fetch(new URL('/health?deep=1', doc2mdUrl), {
      signal: AbortSignal.timeout(5_000),
    });
    record('민규 doc2md', response.ok ? 'ready' : 'error', `${doc2mdUrl} HTTP ${response.status}`);
  } catch (error) {
    record('민규 doc2md', 'error', error instanceof Error ? error.message : String(error));
  }
}

for (const result of results) {
  const mark = result.status === 'ready' ? '✓' : result.status === 'pending' ? '○' : '✗';
  console.log(`${mark} ${result.name}: ${result.detail}`);
}

const hasError = results.some((result) => result.status === 'error');
const hasPending = results.some((result) => result.status === 'pending');
if (hasError || (strict && hasPending)) process.exitCode = 1;
