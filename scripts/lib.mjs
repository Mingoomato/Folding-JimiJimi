import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const localDir = path.join(rootDir, 'codegate-2026-local');
export const agentDir = path.join(rootDir, 'codegate-2026-agent');
export const convertDir = path.join(rootDir, 'codegate-2026-convert', 'services', 'doc2md');
export const backendDir = path.join(rootDir, 'codegate-2026-backend');
export const cloudApiDir = path.join(backendDir, 'cloud-api');
export const wikiBuilderDir = path.join(backendDir, 'wiki-builder');
export const localRuntimeDir = path.join(backendDir, 'local-runtime');
export const isWindows = process.platform === 'win32';

export function executable(name) {
  if (!isWindows) return name;
  return name === 'pnpm' ? 'pnpm.cmd' : `${name}.exe`;
}

export function venvExecutable(projectDir, name) {
  return path.join(projectDir, '.venv', isWindows ? 'Scripts' : 'bin', executable(name));
}

export function nodeModulesExecutable(projectDir, name) {
  return path.join(projectDir, 'node_modules', '.bin', isWindows ? `${name}.cmd` : name);
}

export function ensureNode22() {
  const major = Number(process.versions.node.split('.')[0]);
  if (!Number.isInteger(major) || major < 22) {
    throw new Error(`Node.js 22 이상이 필요합니다. 현재 버전: ${process.version}`);
  }
}

export function requirePath(candidate, label) {
  if (!existsSync(candidate)) throw new Error(`${label}을 찾을 수 없습니다: ${candidate}`);
}

export function run(command, args, { cwd = rootDir, env = process.env, name = command } = {}) {
  return new Promise((resolve, reject) => {
    console.log(`\n[${name}] ${command} ${args.join(' ')}`);
    const child = spawn(command, args, {
      cwd,
      env,
      stdio: 'inherit',
      shell: isWindows && command.toLowerCase().endsWith('.cmd'),
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${name} 실패: ${signal ?? `종료 코드 ${code}`}`));
    });
  });
}
