/**
 * 스펙 v1.3 §5 "원본 안전" — L3 백업 → 원자쓰기 규칙을 테스트로 고정한다.
 * 패치 도중 프로세스가 죽는 시나리오를 훅으로 재현해, 원본이 잘리지 않음을 확인한다.
 */
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { atomicReplaceFile } from '@main/kordoc/atomic';
import { MockKordoc, MOCK_UNAPPLIED_MARK } from '@main/kordoc/mock';

const ORIGINAL = '원본 내용\n두 번째 줄\n';

let dir: string;
let target: string;

beforeEach(async () => {
  dir = await mkdtemp(path.join(tmpdir(), 'codegate-kordoc-'));
  target = path.join(dir, '계약서.md');
  await writeFile(target, ORIGINAL, 'utf8');
});

afterEach(async () => {
  await rm(dir, { recursive: true, force: true });
});

describe('L3 patch — 백업 → 임시파일 → rename', () => {
  it('성공하면 원본이 교체되고 .bak 에 이전 내용이 남는다', async () => {
    const kordoc = new MockKordoc();
    const result = await kordoc.patch(target, '수정된 내용\n');

    expect(result.ok).toBe(true);
    expect(result.exitCode).toBe(0);
    expect(await readFile(target, 'utf8')).toBe('수정된 내용\n');
    expect(await readFile(result.backupPath, 'utf8')).toBe(ORIGINAL);
    expect(result.backupPath).toBe(`${target}.bak`);
  });

  it('rename 전에는 원본이 그대로다 (원자성)', async () => {
    const kordoc = new MockKordoc();
    let contentAtTempWrite = '';

    await kordoc.patch(target, '새 내용\n', {
      afterTempWrite: async () => {
        contentAtTempWrite = await readFile(target, 'utf8');
      },
    });

    expect(contentAtTempWrite).toBe(ORIGINAL);
    expect(await readFile(target, 'utf8')).toBe('새 내용\n');
  });

  it('백업 직후 프로세스가 죽어도 원본은 온전하다', async () => {
    const kordoc = new MockKordoc();

    await expect(
      kordoc.patch(target, '절대 반영되면 안 되는 내용\n', {
        afterBackup: () => {
          throw new Error('SIGKILL 시뮬레이션');
        },
      }),
    ).rejects.toThrow('SIGKILL 시뮬레이션');

    expect(await readFile(target, 'utf8')).toBe(ORIGINAL);
    expect(await readFile(`${target}.bak`, 'utf8')).toBe(ORIGINAL);
  });

  it('임시파일 작성 직후(rename 직전) 죽어도 원본은 잘리지 않는다', async () => {
    const kordoc = new MockKordoc();

    await expect(
      kordoc.patch(target, '절대 반영되면 안 되는 내용\n', {
        afterTempWrite: () => {
          throw new Error('SIGKILL 시뮬레이션');
        },
      }),
    ).rejects.toThrow('SIGKILL 시뮬레이션');

    // 원본은 이전 내용 그대로 — 잘린 파일이 남지 않는다
    expect(await readFile(target, 'utf8')).toBe(ORIGINAL);
    expect(await readFile(`${target}.bak`, 'utf8')).toBe(ORIGINAL);

    // 남은 임시파일은 숨김(`.`) 이름이라 원본을 가리지 않는다
    const leftovers = (await readdir(dir)).filter((n) => n.includes('.kordoc-'));
    for (const name of leftovers) expect(name.startsWith('.')).toBe(true);
  });

  it('exit 2(부분 실패)는 성공으로 취급되지 않는다', async () => {
    const kordoc = new MockKordoc();
    const edited = ['적용되는 줄', `제3조 대금 지급 ${MOCK_UNAPPLIED_MARK}`, '또 적용되는 줄'].join(
      '\n',
    );

    const result = await kordoc.patch(target, edited);

    expect(result.ok).toBe(false);
    expect(result.exitCode).toBe(2);
    expect(result.unapplied).toHaveLength(1);
    expect(result.unapplied[0]).toContain('제3조');
    // 적용된 편집이 있으므로 문서는 갱신되고, 백업은 남는다
    expect(await readFile(result.backupPath, 'utf8')).toBe(ORIGINAL);
    expect(await readFile(target, 'utf8')).not.toContain(MOCK_UNAPPLIED_MARK);
  });

  it('편집을 하나도 적용하지 못하면 원본을 덮어쓰지 않는다', async () => {
    const kordoc = new MockKordoc();

    await expect(kordoc.patch(target, `${MOCK_UNAPPLIED_MARK}`)).rejects.toThrow(
      /적용하지 못했습니다/,
    );
    expect(await readFile(target, 'utf8')).toBe(ORIGINAL);
  });

  it('없는 파일은 한국어 오류로 거절한다', async () => {
    const kordoc = new MockKordoc();
    await expect(kordoc.patch(path.join(dir, '없는파일.hwpx'), '내용')).rejects.toThrow(
      /찾을 수 없습니다/,
    );
  });
});

describe('L3 render — hwpx 전용', () => {
  it('.hwp 는 한국어 오류로 거절한다', async () => {
    const kordoc = new MockKordoc();
    const hwp = path.join(dir, '계약서.hwp');
    await writeFile(hwp, 'dummy');
    await expect(kordoc.render(hwp)).rejects.toThrow(/hwpx/);
  });

  it('.hwpx 는 페이지 수만큼 버퍼를 돌려준다', async () => {
    const kordoc = new MockKordoc();
    const hwpx = path.join(dir, '계약서.hwpx');
    await writeFile(hwpx, 'dummy');
    const pages = await kordoc.render(hwpx, [1, 2, 3]);
    expect(pages).toHaveLength(3);
    expect(pages[0]!.length).toBeGreaterThan(0);
  });
});

describe('atomicReplaceFile 순서', () => {
  it('백업 → 임시파일 → rename 순서로 진행한다', async () => {
    const order: string[] = [];

    await atomicReplaceFile(
      target,
      async (tempPath) => {
        order.push('produce');
        await writeFile(tempPath, '새 문서\n', 'utf8');
      },
      {
        afterBackup: async () => {
          order.push('backup');
          expect(await readFile(`${target}.bak`, 'utf8')).toBe(ORIGINAL);
        },
        afterTempWrite: async () => {
          order.push('tempWritten');
          expect(await readFile(target, 'utf8')).toBe(ORIGINAL);
        },
      },
    );

    expect(order).toEqual(['backup', 'produce', 'tempWritten']);
    expect(await readFile(target, 'utf8')).toBe('새 문서\n');
  });
});
