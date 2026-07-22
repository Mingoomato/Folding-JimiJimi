#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { loadEnvFile } from 'node:process';

import {
  agentDir,
  backendDir,
  cloudApiDir,
  convertDir,
  ensureNode22,
  executable,
  localDir,
  requirePath,
  rootDir,
  venvExecutable,
} from './lib.mjs';
import { buildRuntimeEnvironments, FIXED_CALLBACK } from './runtime-environment.mjs';

// The nested desktop supervisor may need up to 10 seconds to TERM/KILL its own
// detached Electron and doc2md groups. Keep its supervisor alive beyond that window.
const CHILD_TERM_GRACE_MS = 15_000;
const envFile = path.join(rootDir, '.env');
ensureNode22();
requirePath(envFile, '.env');
loadEnvFile(envFile);

for (const name of [
  'GEMINI_API_KEY',
  'CODEGATE_GOOGLE_CLIENT_ID',
  'CODEGATE_GOOGLE_CLIENT_SECRET',
]) {
  if (!process.env[name]?.trim()) throw new Error(`.env의 ${name} 값을 입력해 주세요.`);
}
if (!process.env.CODEGATE_SESSION_PEPPER || process.env.CODEGATE_SESSION_PEPPER.length < 32) {
  throw new Error('.env의 CODEGATE_SESSION_PEPPER는 32자 이상이어야 합니다. pnpm setup을 다시 실행하세요.');
}
if (process.env.CODEGATE_OAUTH_REDIRECT_URI !== FIXED_CALLBACK) {
  throw new Error(`CODEGATE_OAUTH_REDIRECT_URI는 ${FIXED_CALLBACK} 이어야 합니다.`);
}
if (process.env.CODEGATE_GEMINI_MODEL !== 'gemini-2.5-flash-lite') {
  throw new Error('CODEGATE_GEMINI_MODEL은 gemini-2.5-flash-lite 이어야 합니다.');
}
if (process.env.CODEGATE_GEMINI_DATA_POLICY !== 'paid-no-training') {
  throw new Error('내부 문서는 결제 프로젝트에서만 전송합니다: CODEGATE_GEMINI_DATA_POLICY=paid-no-training');
}

const sidecarBin = venvExecutable(agentDir, 'codegate-local');
const doc2mdPython = venvExecutable(convertDir, 'python');
requirePath(sidecarBin, 'Agent sidecar (먼저 pnpm setup 실행)');
requirePath(doc2mdPython, 'doc2md Python (먼저 pnpm setup 실행)');
requirePath(venvExecutable(cloudApiDir, 'codegate-cloud-api'), 'Cloud API (먼저 pnpm setup 실행)');

const runtimeDir = path.join(rootDir, '.runtime');
await mkdir(runtimeDir, { recursive: true });
const { cloudEnv, desktopEnv } = buildRuntimeEnvironments(process.env, {
  runtimeDir,
  convertDir,
  doc2mdPython,
  sidecarBin,
  backendDir,
  localDir,
});

const children = [];
let stopping = false;
function start(command, args, options) {
  const child = spawn(command, args, {
    ...options,
    stdio: 'inherit',
    detached: process.platform !== 'win32',
    shell: process.platform === 'win32' && command.toLowerCase().endsWith('.cmd'),
  });
  child.spawnError = null;
  children.push(child);
  child.once('error', (error) => {
    child.spawnError = error;
    if (!stopping) console.error(error);
  });
  return child;
}

async function stopAll() {
  if (stopping) return;
  stopping = true;
  await Promise.all([...children].reverse().map(stop));
}

const removeSignalHandlers = installSignalCleanup(stopAll);

try {
  const cloud = start(
    executable('uv'),
    ['run', '--project', cloudApiDir, '--locked', 'codegate-cloud-api'],
    { cwd: cloudApiDir, env: cloudEnv },
  );
  await waitForHealth('http://127.0.0.1:8000/api/v1/health', cloud, 30_000);
  console.log('✓ Google OAuth cloud API 준비 완료');

  const desktop = start(process.execPath, [path.join(localDir, 'scripts', 'dev-integrated.mjs')], {
    cwd: localDir,
    env: desktopEnv,
  });
  const first = await Promise.race([
    waitForExit(desktop).then((exit) => ({ name: 'Electron', exit })),
    waitForExit(cloud).then((exit) => ({ name: 'cloud-api', exit })),
  ]);
  if (first.name === 'cloud-api') {
    throw new Error(`cloud-api가 먼저 종료됐습니다: ${describeExit(first.exit)}`);
  }
  if (
    first.exit.code !== 0 &&
    first.exit.signal !== 'SIGINT' &&
    first.exit.signal !== 'SIGTERM'
  ) {
    throw new Error(`Electron이 ${describeExit(first.exit)}로 종료됐습니다.`);
  }
} finally {
  removeSignalHandlers();
  await stopAll();
}

async function waitForHealth(url, child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = '응답 없음';
  while (Date.now() < deadline) {
    if (child.spawnError) throw child.spawnError;
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error('cloud-api가 준비 전에 종료됐습니다.');
    }
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(3_000) });
      const body = await response.json();
      if (response.ok && body.status === 'ok' && body.oauth_provider === 'google') {
        if (child.exitCode !== null || child.signalCode !== null) {
          throw new Error('cloud-api가 health 응답 직후 종료됐습니다.');
        }
        return;
      }
      lastError = `HTTP ${response.status}: ${JSON.stringify(body)}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`cloud-api 준비 시간 초과: ${lastError}`);
}

function waitForExit(child) {
  if (child.spawnError) return Promise.reject(child.spawnError);
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });
}

function describeExit(exit) {
  return exit.signal ?? `종료 코드 ${exit.code}`;
}

async function stop(child) {
  if (!processTreeRunning(child)) return;
  await signalProcessTree(child, 'SIGTERM');
  if (await waitForProcessTreeExit(child, CHILD_TERM_GRACE_MS)) return;
  await signalProcessTree(child, 'SIGKILL');
  if (!(await waitForProcessTreeExit(child, 5_000))) {
    throw new Error(`process tree ${child.pid ?? 'unknown'}를 종료하지 못했습니다.`);
  }
}

async function waitForProcessTreeExit(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processTreeRunning(child)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return !processTreeRunning(child);
}

function processTreeRunning(child) {
  if (process.platform !== 'win32' && child.pid) {
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
  if (process.platform !== 'win32') {
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
    const killer = spawn('taskkill.exe', args, { stdio: 'ignore' });
    killer.once('error', resolve);
    killer.once('exit', resolve);
  });
}

function installSignalCleanup(cleanup) {
  const handlers = new Map();
  for (const [signal, exitCode] of [
    ['SIGINT', 130],
    ['SIGTERM', 143],
  ]) {
    const handler = () => {
      void cleanup().finally(() => process.exit(exitCode));
    };
    handlers.set(signal, handler);
    process.once(signal, handler);
  }
  return () => {
    for (const [signal, handler] of handlers) process.off(signal, handler);
  };
}
