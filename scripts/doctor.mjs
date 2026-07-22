#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { loadEnvFile } from 'node:process';

import {
  agentDir,
  cloudApiDir,
  convertDir,
  ensureNode22,
  executable,
  isWindows,
  localDir,
  rootDir,
  venvExecutable,
} from './lib.mjs';

const FIXED_CALLBACK = 'http://127.0.0.1:47821/auth/callback';
const envFile = path.join(rootDir, '.env');
const checks = [];

function check(label, ok, detail) {
  checks.push({ label, ok, detail });
}

try {
  ensureNode22();
  check('Node.js', true, process.version);
} catch (error) {
  check('Node.js', false, error.message);
}

for (const command of ['pnpm', 'uv']) {
  const executablePath = executable(command);
  const result = spawnSync(executablePath, ['--version'], {
    encoding: 'utf8',
    shell: isWindows && executablePath.toLowerCase().endsWith('.cmd'),
  });
  check(command, result.status === 0, result.status === 0 ? result.stdout.trim() : '실행 불가');
}

check('.env', existsSync(envFile), envFile);
if (existsSync(envFile)) loadEnvFile(envFile);
for (const name of [
  'GEMINI_API_KEY',
  'CODEGATE_GOOGLE_CLIENT_ID',
  'CODEGATE_GOOGLE_CLIENT_SECRET',
]) {
  check(name, Boolean(process.env[name]?.trim()), process.env[name]?.trim() ? '설정됨' : '비어 있음');
}
check(
  'Session pepper',
  Boolean(process.env.CODEGATE_SESSION_PEPPER && process.env.CODEGATE_SESSION_PEPPER.length >= 32),
  process.env.CODEGATE_SESSION_PEPPER?.length >= 32 ? '설정됨' : '32자 미만 또는 비어 있음',
);
check(
  'OAuth callback',
  process.env.CODEGATE_OAUTH_REDIRECT_URI === FIXED_CALLBACK,
  process.env.CODEGATE_OAUTH_REDIRECT_URI ?? '미설정',
);
check(
  'Gemini model',
  process.env.CODEGATE_GEMINI_MODEL === 'gemini-2.5-flash-lite',
  process.env.CODEGATE_GEMINI_MODEL ?? '미설정',
);
check(
  'Gemini data policy',
  process.env.CODEGATE_GEMINI_DATA_POLICY === 'paid-no-training',
  process.env.CODEGATE_GEMINI_DATA_POLICY ?? '미설정',
);
check('Electron 설치', existsSync(path.join(localDir, 'node_modules')), localDir);
check('Agent 설치', existsSync(venvExecutable(agentDir, 'codegate-local')), agentDir);
check('Cloud API 설치', existsSync(venvExecutable(cloudApiDir, 'codegate-cloud-api')), cloudApiDir);
check('doc2md 설치', existsSync(venvExecutable(convertDir, 'python')), convertDir);

for (const item of checks) {
  console.log(`${item.ok ? '✓' : '✗'} ${item.label}: ${item.detail}`);
}

if (checks.some((item) => !item.ok)) process.exitCode = 1;
