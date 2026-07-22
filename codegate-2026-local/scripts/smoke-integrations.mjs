#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { safeProcessEnvironment } from './safe-process-environment.mjs';

const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const workspaceDir = path.resolve(projectDir, '..');
const isWindows = process.platform === 'win32';
const executable = isWindows ? 'codegate-local.exe' : 'codegate-local';
const python = isWindows ? 'python.exe' : 'python';

const backendBin =
  process.env.CODEGATE_SIDECAR_BIN ??
  path.join(workspaceDir, 'Backend', '.venv', isWindows ? 'Scripts' : 'bin', executable);
const llmwikiRoot =
  process.env.CODEGATE_LLMWIKI_PROJECT_ROOT ?? path.join(workspaceDir, 'LLMWIKI');
const doc2mdRoot =
  process.env.CODEGATE_DOC2MD_PROJECT_ROOT ??
  path.join(workspaceDir, 'codegate-2026-api', 'services', 'doc2md');
const configuredDoc2mdPython = process.env.CODEGATE_DOC2MD_PYTHON;
const doc2mdCommand = configuredDoc2mdPython ?? 'uv';
const doc2mdCommandPrefix = configuredDoc2mdPython
  ? []
  : ['run', '--isolated', '--python', '3.12', '--project', doc2mdRoot, python];

const runtimeRoot = await mkdtemp(path.join(os.tmpdir(), 'codegate-integration-smoke-'));
const sourceRoot = path.join(runtimeRoot, 'source');
const inputRoot = path.join(runtimeRoot, 'source-md');
const storageRoot = path.join(runtimeRoot, 'llmwiki-storage');
const dataRoot = path.join(runtimeRoot, 'app-data');
const fixtureRoot = path.join(projectDir, 'tests', 'fixtures', 'sidecar');
const children = [];
let cleanupPromise = null;
const cleanup = () =>
  (cleanupPromise ??= Promise.all([...children].reverse().map(stop)).then(() =>
    rm(runtimeRoot, { recursive: true, force: true }),
  ));
const removeSignalHandlers = installSignalCleanup(cleanup);

try {
  await Promise.all([
    cp(path.join(fixtureRoot, 'source'), sourceRoot, { recursive: true }),
    mkdir(inputRoot, { recursive: true }),
    mkdir(storageRoot, { recursive: true }),
    mkdir(dataRoot, { recursive: true }),
  ]);

  const doc2mdPort = await allocatePort();
  const doc2md = start(
    doc2mdCommand,
    [
      ...doc2mdCommandPrefix,
      '-m',
      'uvicorn',
      'app.main:app',
      '--host',
      '127.0.0.1',
      '--port',
      String(doc2mdPort),
    ],
    { cwd: doc2mdRoot, name: 'doc2md' },
  );
  children.push(doc2md);
  const doc2mdUrl = `http://127.0.0.1:${doc2mdPort}`;
  await waitForJson(
    `${doc2mdUrl}/health?deep=1`,
    (health) => health.status === 'ok',
    doc2md,
    180_000,
    120_000,
  );
  const sourceRelativePath = 'regulations/REG-100001.md';
  // `pnpm exec`는 실행 전에 lockfile 정책을 다시 평가해, 이미 설치·검증한 fixture test가
  // 신규 패키지 숙성기간 설정 때문에 실패할 수 있다. 설치된 고정 binary를 직접 사용한다.
  const vitestBin = path.join(
    projectDir,
    'node_modules',
    '.bin',
    isWindows ? 'vitest.cmd' : 'vitest',
  );
  const inputSync = start(
    vitestBin,
    ['run', 'tests/integration-input-sync.test.ts'],
    {
      cwd: projectDir,
      name: 'production-input-sync',
      env: {
        CODEGATE_INPUT_SYNC_INTEGRATION: '1',
        CODEGATE_INPUT_SYNC_SOURCE: path.join(sourceRoot, sourceRelativePath),
        CODEGATE_INPUT_SYNC_TARGET: inputRoot,
        CODEGATE_INPUT_SYNC_RELATIVE_PATH: sourceRelativePath,
        CODEGATE_DOC2MD_URL: doc2mdUrl,
      },
    },
  );
  children.push(inputSync);
  await waitForProcess(inputSync);
  const normalizedFiles = (await readdir(inputRoot)).filter((name) => name.endsWith('.md'));
  assert(normalizedFiles.length === 1, 'production input sync가 Markdown 하나를 만들지 않았습니다.');
  const normalized = await readFile(path.join(inputRoot, normalizedFiles[0]), 'utf8');
  const documentId = normalized.match(/"id":\s*"([A-Z0-9-]+)"/)?.[1];
  assert(documentId, 'production input sync가 stable document ID를 만들지 않았습니다.');

  const sidecarPort = await allocatePort();
  const sidecar = start(
    backendBin,
    [
      '--source-root',
      sourceRoot,
      '--llmwiki-project-root',
      llmwikiRoot,
      '--llmwiki-input-root',
      inputRoot,
      '--llmwiki-storage-root',
      storageRoot,
      '--data-root',
      dataRoot,
      '--port',
      String(sidecarPort),
      '--cors-origin',
      'app://codegate',
      '--doc2md-url',
      doc2mdUrl,
      '--deterministic-agent',
    ],
    {
      cwd: projectDir,
      name: 'sidecar',
      env: {
        CODEGATE_BOOTSTRAP_DEMO: 'false',
        CODEGATE_KNOWLEDGE_MODE: 'llmwiki',
        CODEGATE_SOURCE_ROOT: sourceRoot,
        CODEGATE_RUNTIME_ROOT: dataRoot,
        CODEGATE_LLMWIKI_PROJECT_ROOT: llmwikiRoot,
        CODEGATE_LLMWIKI_INPUT_ROOT: inputRoot,
        CODEGATE_LLMWIKI_STORAGE_ROOT: storageRoot,
        CODEGATE_CORS_ORIGINS: '["app://codegate"]',
        CODEGATE_AGENT_MODE: 'deterministic',
        CODEGATE_DOC2MD_BASE_URL: doc2mdUrl,
      },
    },
  );
  children.push(sidecar);
  const api = `http://127.0.0.1:${sidecarPort}/api/v1`;
  await waitForJson(
    `${api}/health`,
    (health) =>
      health.status === 'ok' &&
      health.agent_available === true &&
      health.converter_available === true,
    sidecar,
  );

  const located = await postJson(
    `${api}/chat/messages`,
    {
      conversation_id: 'integration-smoke',
      message: '개인정보 보관 기간 문서 어디 있어?',
    },
    'smoke-locate-0001',
  );
  assert(
    located.response_type === 'location_result' &&
      located.documents?.[0]?.document_id === documentId,
    `위치 검색이 ${documentId} 근거를 반환하지 않았습니다.`,
  );
  const beforeVersion = located.documents[0].graph_version;

  const preview = await postJson(
    `${api}/chat/messages`,
    {
      conversation_id: 'integration-smoke',
      message: `${documentId}에서 "1년"을 "3년"으로 변경해줘`,
      selected_document_id: documentId,
    },
    'smoke-change-0001',
  );
  const plan = preview.change_plan;
  assert(
    preview.response_type === 'change_preview' &&
      plan?.unified_diff?.includes('+개인정보는 3년간 보관한다.'),
    '변경 요청이 exact diff를 반환하지 않았습니다.',
  );

  const approved = await postJson(
    `${api}/change-plans/${plan.change_plan_id}/approve`,
    { plan_hash: plan.plan_hash },
    'smoke-approve-0001',
  );
  const completed = await waitForExecution(api, approved.execution_id);
  assert(
    completed.status === 'completed' && completed.sync_status === 'published',
    `승인 실행이 publish되지 않았습니다: ${completed.status}/${completed.sync_status}`,
  );
  assert(
    (await readFile(path.join(sourceRoot, 'regulations', 'REG-100001.md'), 'utf8')).includes(
      '3년간',
    ),
    '승인된 원본 변경이 적용되지 않았습니다.',
  );
  assert(
    completed.graph_version_after && completed.graph_version_after !== beforeVersion,
    '승인 뒤 LLMWIKI version이 바뀌지 않았습니다.',
  );

  const undoStarted = await postJson(
    `${api}/executions/${approved.execution_id}/undo`,
    undefined,
    'smoke-undo-0001',
  );
  const undone = await waitForExecution(api, undoStarted.execution_id);
  assert(
    undone.status === 'completed' &&
      undone.sync_status === 'published' &&
      undone.undo_of_execution_id === approved.execution_id,
    `Undo 실행이 publish되지 않았습니다: ${undone.status}/${undone.sync_status}`,
  );
  const originalAfterUndo = await getJson(`${api}/executions/${approved.execution_id}`);
  assert(originalAfterUndo.status === 'undone', '원본 실행이 undone 상태로 전이되지 않았습니다.');
  assert(
    (await readFile(path.join(sourceRoot, 'regulations', 'REG-100001.md'), 'utf8')).includes(
      '1년간',
    ),
    'Undo가 원본을 복구하지 않았습니다.',
  );
  assert(
    undone.graph_version_after && undone.graph_version_after !== completed.graph_version_after,
    'Undo 뒤 LLMWIKI version이 바뀌지 않았습니다.',
  );

  console.log('✓ 원본 → 민규 doc2md → LLMWIKI normalized input');
  console.log('✓ 성주 sidecar + 용휘 LLMWIKI health');
  console.log(`✓ 우창 HTTP 계약으로 ${documentId} 위치 검색`);
  console.log('✓ exact diff 승인 → 원본 변경 → 새 LLMWIKI version publish');
  console.log('✓ Undo → 원본 복구 → 후속 LLMWIKI version publish');
} catch (error) {
  for (const child of children) {
    if (child.output.trim()) {
      console.error(`\n[${child.name} 마지막 출력]\n${child.output.slice(-6_000)}`);
    }
  }
  throw error;
} finally {
  removeSignalHandlers();
  await cleanup();
}

function allocatePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        server.close();
        reject(new Error('빈 loopback port를 할당하지 못했습니다.'));
        return;
      }
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}

function start(command, args, { cwd, name, env = {} }) {
  const child = spawn(command, args, {
    cwd,
    detached: !isWindows,
    shell: isWindows && command.toLowerCase().endsWith('.cmd'),
    env: {
      ...safeProcessEnvironment(process.env),
      CODEGATE_ENVIRONMENT: 'local',
      CODEGATE_AUTH_MODE: 'local',
      ...env,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const tracked = { child, name, output: '', spawnError: null };
  const collect = (chunk) => {
    tracked.output = `${tracked.output}${chunk.toString()}`.slice(-12_000);
  };
  child.stdout.on('data', collect);
  child.stderr.on('data', collect);
  child.once('error', (error) => {
    tracked.spawnError = error;
    collect(`spawn error: ${error.message}\n`);
  });
  return tracked;
}

function waitForProcess(processInfo) {
  if (processInfo.spawnError) return Promise.reject(processInfo.spawnError);
  if (processInfo.child.exitCode !== null) {
    return processInfo.child.exitCode === 0
      ? Promise.resolve()
      : Promise.reject(new Error(`${processInfo.name} 종료 코드 ${processInfo.child.exitCode}`));
  }
  return new Promise((resolve, reject) => {
    processInfo.child.once('error', reject);
    processInfo.child.once('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${processInfo.name} 종료 코드 ${code ?? signal}`));
    });
  });
}

async function waitForJson(
  url,
  predicate,
  processInfo,
  timeoutMs = 120_000,
  requestTimeoutMs = 2_000,
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = '응답 없음';
  while (Date.now() < deadline) {
    if (processInfo.spawnError) {
      throw new Error(`${processInfo.name} process를 시작하지 못했습니다: ${processInfo.spawnError.message}`);
    }
    if (processInfo.child.exitCode !== null || processInfo.child.signalCode !== null) {
      throw new Error(`${processInfo.name} process가 준비되기 전에 종료됐습니다.`);
    }
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(requestTimeoutMs) });
      const body = await response.json();
      if (response.ok && predicate(body)) return body;
      lastError = `HTTP ${response.status}: ${JSON.stringify(body)}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await delay(250);
  }
  throw new Error(`${processInfo.name} 준비 시간이 초과됐습니다: ${lastError}`);
}

async function postJson(url, body, idempotencyKey) {
  const headers = { 'idempotency-key': idempotencyKey };
  const init = { method: 'POST', headers, signal: AbortSignal.timeout(30_000) };
  if (body !== undefined) {
    headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(url, init);
  const payload = await response.json();
  assert(response.ok, `${url} 요청 실패: HTTP ${response.status} ${JSON.stringify(payload)}`);
  return payload;
}

async function getJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(5_000) });
  const payload = await response.json();
  assert(response.ok, `${url} 요청 실패: HTTP ${response.status} ${JSON.stringify(payload)}`);
  return payload;
}

async function waitForExecution(api, executionId, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  let current = null;
  while (Date.now() < deadline) {
    const response = await fetch(`${api}/executions/${executionId}`, {
      signal: AbortSignal.timeout(5_000),
    });
    current = await response.json();
    assert(response.ok, `execution 조회 실패: HTTP ${response.status} ${JSON.stringify(current)}`);
    if (current.terminal) return current;
    await delay(Math.min(Math.max(current.recommended_poll_after_ms ?? 250, 100), 2_000));
  }
  throw new Error(`execution ${executionId} 완료 시간이 초과됐습니다: ${JSON.stringify(current)}`);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function stop({ child }) {
  if (!processTreeRunning(child)) return;
  await signalProcessTree(child, 'SIGTERM');
  if (await waitForProcessTreeExit(child, 5_000)) return;
  await signalProcessTree(child, 'SIGKILL');
  if (!(await waitForProcessTreeExit(child, 5_000))) {
    throw new Error(`process tree ${child.pid ?? 'unknown'}를 종료하지 못했습니다.`);
  }
}

async function waitForProcessTreeExit(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processTreeRunning(child)) return true;
    await delay(100);
  }
  return !processTreeRunning(child);
}

function processTreeRunning(child) {
  if (!isWindows && child.pid) {
    try {
      process.kill(-child.pid, 0);
      return true;
    } catch (error) {
      return error?.code !== 'ESRCH';
    }
  }
  return child.exitCode === null && child.signalCode === null;
}

async function signalProcessTree(child, signal) {
  if (!child.pid) return;
  if (!isWindows) {
    try {
      process.kill(-child.pid, signal);
      return;
    } catch {
      try {
        child.kill(signal);
      } catch {
        return;
      }
    }
    return;
  }
  await new Promise((resolve) => {
    const args = ['/pid', String(child.pid), '/t'];
    if (signal === 'SIGKILL') args.push('/f');
    const killer = spawn('taskkill.exe', args, {
      stdio: 'ignore',
    });
    killer.once('error', () => resolve());
    killer.once('exit', () => resolve());
  }).catch(() => undefined);
}

function installSignalCleanup(cleanupAction) {
  const handlers = new Map();
  for (const [signal, exitCode] of [
    ['SIGINT', 130],
    ['SIGTERM', 143],
  ]) {
    const handler = () => {
      void cleanupAction().finally(() => process.exit(exitCode));
    };
    handlers.set(signal, handler);
    process.once(signal, handler);
  }
  return () => {
    for (const [signal, handler] of handlers) process.off(signal, handler);
  };
}
