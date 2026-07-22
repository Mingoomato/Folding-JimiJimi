/**
 * 파일시스템 유틸 — 해시·원자적 쓰기.
 * 스펙 v1.3 §5 "원본 안전" / "위키 전개는 원자적으로" 를 구현하는 최하위 계층이다.
 * electron 을 import 하지 않는다 (vitest 에서 그대로 테스트 가능해야 한다).
 */
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

/** 버퍼/문자열의 sha256 hex. */
export function sha256(data: Buffer | string): string {
  return createHash('sha256').update(data).digest('hex');
}

/** 파일 내용의 sha256 hex (스트리밍 — 큰 파일도 안전). */
export function sha256File(filePath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256');
    const stream = createReadStream(filePath);
    stream.on('error', reject);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

export async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

export async function pathExists(target: string): Promise<boolean> {
  try {
    await fs.stat(target);
    return true;
  } catch {
    return false;
  }
}

/** 재귀 삭제 — 없으면 조용히 통과. */
export async function rmrf(target: string): Promise<void> {
  await fs.rm(target, { recursive: true, force: true });
}

/**
 * 같은 디렉터리에 임시파일을 쓰고 rename 으로 덮어쓴다.
 * 도중에 프로세스가 죽어도 대상 파일은 "이전 내용" 또는 "완전한 새 내용" 둘 중 하나다.
 */
export async function writeFileAtomic(target: string, data: Buffer | string): Promise<void> {
  const dir = path.dirname(target);
  await ensureDir(dir);
  const tmp = path.join(dir, `.${path.basename(target)}.tmp-${randomUUID()}`);
  const handle = await fs.open(tmp, 'w');
  try {
    await handle.writeFile(data);
    await handle.sync(); // 디스크까지 내려간 뒤에 rename 해야 원자성이 의미를 갖는다
  } finally {
    await handle.close();
  }
  await fs.rename(tmp, target);
}

/** JSON 을 원자적으로 저장. */
export async function writeJsonAtomic(target: string, value: unknown): Promise<void> {
  await writeFileAtomic(target, `${JSON.stringify(value, null, 2)}\n`);
}

/** JSON 읽기 — 없거나 깨졌으면 null. */
export async function readJson<T>(target: string): Promise<T | null> {
  try {
    const raw = await fs.readFile(target, 'utf8');
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/** 디렉터리를 재귀 순회하며 파일 절대경로를 모은다. */
export async function walkFiles(
  dir: string,
  opts: { skipDir?: (name: string) => boolean } = {},
): Promise<string[]> {
  const out: string[] = [];
  const stack = [dir];
  while (stack.length > 0) {
    const current = stack.pop()!;
    let entries: import('node:fs').Dirent[];
    try {
      entries = await fs.readdir(current, { withFileTypes: true });
    } catch {
      continue; // 권한 없는 디렉터리는 건너뛴다
    }
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (opts.skipDir?.(entry.name)) continue;
        stack.push(full);
      } else if (entry.isFile()) {
        out.push(full);
      }
    }
  }
  return out.sort();
}
