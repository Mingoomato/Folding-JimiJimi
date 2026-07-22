/**
 * L3 원본 안전 규칙 (스펙 v1.3 §1 L3 · §5 "원본 안전").
 *
 *   ① `.bak` 백업  →  ② 같은 디렉터리에 임시파일 작성  →  ③ rename 으로 덮어쓰기(원자적)
 *
 * 어느 단계에서 프로세스가 죽더라도 원본 경로에는 "이전 파일" 또는 "완전한 새 파일"만
 * 존재한다. 잘린(truncated) 파일이 남는 경우는 없다.
 * 테스트(`tests/kordoc-atomic.test.ts`)가 이 순서를 고정한다.
 */
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

/** 프로세스 킬 시뮬레이션용 훅 — 테스트에서만 사용한다. */
export interface AtomicReplaceHooks {
  /** 백업 직후 (임시파일 작성 전) */
  afterBackup?: () => void | Promise<void>;
  /** 임시파일 작성 직후 (rename 전) */
  afterTempWrite?: () => void | Promise<void>;
}

export interface AtomicReplaceResult {
  /** 생성된 `.bak` 백업 경로 */
  backupPath: string;
  /** 사용된 임시파일 경로 (rename 후에는 존재하지 않는다) */
  tempPath: string;
}

/**
 * `target` 을 원자적으로 교체한다.
 * `produce(tempPath)` 는 새 내용을 임시 경로에 만들어 놓기만 하면 된다.
 * `produce` 가 던지면 임시파일을 정리하고 원본은 손대지 않는다.
 */
export async function atomicReplaceFile(
  target: string,
  produce: (tempPath: string) => Promise<void>,
  hooks: AtomicReplaceHooks = {},
): Promise<AtomicReplaceResult> {
  const dir = path.dirname(target);
  const base = path.basename(target);
  const backupPath = `${target}.bak`;
  const tempPath = path.join(dir, `.${base}.kordoc-${randomUUID()}.tmp`);

  // ① 백업 — 원본이 반드시 존재해야 한다
  await fs.copyFile(target, backupPath);
  await hooks.afterBackup?.();

  // ② 임시파일 작성 (같은 디렉터리 = 같은 파일시스템 → rename 이 원자적)
  try {
    await produce(tempPath);
    await fsyncFile(tempPath);
  } catch (err) {
    await fs.rm(tempPath, { force: true });
    throw err;
  }
  await hooks.afterTempWrite?.();

  // ③ rename — 여기서부터는 되돌릴 수 없고, 되돌릴 필요도 없다
  await fs.rename(tempPath, target);

  return { backupPath, tempPath };
}

async function fsyncFile(filePath: string): Promise<void> {
  const handle = await fs.open(filePath, 'r+');
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}
