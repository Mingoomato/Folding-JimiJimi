import { describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
  GENERATED_EXTENSION,
  safeFileName,
  titleFromMarkdown,
  uniquePath,
  writeHwpx,
} from '@main/kordoc/generate';

/**
 * 답변을 한글 문서로 저장하기.
 *
 * 생성은 수정과 성격이 다르다. 수정에는 되돌릴 원본(`.bak`)이 있지만 생성에는 없다.
 * 그래서 **덮어쓰지 않는 것**이 이 코드의 가장 중요한 약속이다.
 */
async function tmpDir(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'folding-gen-'));
}

describe('safeFileName', () => {
  it('경로가 될 수 있는 글자를 걷어낸다', () => {
    // 이걸 놓치면 답변 제목이 그대로 경로 탈출이 된다.
    expect(safeFileName('../../etc/passwd')).not.toContain('/');
    expect(safeFileName('보고서: 1/2 <초안>')).toBe('보고서 1 2 초안');
  });

  it('걷어내고 남는 게 없으면 기본 이름을 쓴다', () => {
    expect(safeFileName('///')).toBe('문서');
    expect(safeFileName('..')).toBe('문서');
    expect(safeFileName('   ')).toBe('문서');
  });

  it('너무 긴 제목은 자른다 — 파일명 길이 제한이 있다', () => {
    expect(safeFileName('가'.repeat(300)).length).toBeLessThanOrEqual(80);
  });
});

describe('titleFromMarkdown', () => {
  it('첫 제목 줄을 문서 이름으로 쓴다', () => {
    expect(titleFromMarkdown('# 자문 계약 요약\n\n본문')).toBe('자문 계약 요약');
    expect(titleFromMarkdown('본문 먼저\n\n## 나중 제목')).toBe('본문 먼저');
  });

  it('제목이 없으면 첫 문장에서 장식을 떼고 쓴다', () => {
    expect(titleFromMarkdown('- 첫째 항목\n- 둘째')).toBe('첫째 항목');
  });

  it('빈 내용이면 기본 이름으로 떨어진다', () => {
    expect(titleFromMarkdown('   \n\n')).toBe('문서');
  });
});

describe('uniquePath', () => {
  it('이름이 겹치면 번호를 붙여 비켜 간다 — 남의 파일을 덮지 않는다', async () => {
    const dir = await tmpDir();
    await fs.writeFile(path.join(dir, '보고서.hwpx'), 'x');

    const next = await uniquePath(dir, '보고서', '.hwpx');
    expect(path.basename(next)).toBe('보고서 (2).hwpx');

    await fs.writeFile(next, 'x');
    expect(path.basename(await uniquePath(dir, '보고서', '.hwpx'))).toBe('보고서 (3).hwpx');
  });
});

describe('writeHwpx', () => {
  const fakeConvert = async (markdown: string) => new TextEncoder().encode(markdown).buffer;

  it('빈 내용으로는 파일을 만들지 않는다', async () => {
    const dir = await tmpDir();
    await expect(
      writeHwpx({ targetPath: path.join(dir, 'a.hwpx'), markdown: '   ', convert: fakeConvert }),
    ).rejects.toThrow(/비어 있어/);
  });

  it('hwpx 가 아닌 확장자는 거부한다 — 형식을 속이지 않는다', async () => {
    const dir = await tmpDir();
    await expect(
      writeHwpx({ targetPath: path.join(dir, 'a.hwp'), markdown: '# 제목', convert: fakeConvert }),
    ).rejects.toThrow(/hwpx/);
  });

  it('이미 있는 파일은 절대 덮지 않는다 — 생성에는 되돌릴 원본이 없다', async () => {
    const dir = await tmpDir();
    const target = path.join(dir, 'a.hwpx');
    await fs.writeFile(target, '원래 내용');

    await expect(
      writeHwpx({ targetPath: target, markdown: '# 새 내용', convert: fakeConvert }),
    ).rejects.toThrow(/이미 있습니다/);
    expect(await fs.readFile(target, 'utf8')).toBe('원래 내용');
  });

  it('변환 결과가 비면 빈 파일을 남기지 않는다', async () => {
    const dir = await tmpDir();
    const target = path.join(dir, 'a.hwpx');
    await expect(
      writeHwpx({ targetPath: target, markdown: '# 제목', convert: async () => new ArrayBuffer(0) }),
    ).rejects.toThrow(/만들지 못했습니다/);
    await expect(fs.access(target)).rejects.toThrow(); // 파일이 생기지 않았다
  });

  it('정상 경로에서는 변환 결과를 그대로 쓴다', async () => {
    const dir = await tmpDir();
    const target = path.join(dir, `문서${GENERATED_EXTENSION}`);
    await writeHwpx({ targetPath: target, markdown: '# 제목', convert: fakeConvert });
    expect(await fs.readFile(target, 'utf8')).toBe('# 제목');
  });
});

/**
 * 실물 kordoc 으로 만든 hwpx 를 **다시 읽어** 내용이 살아 있는지 본다.
 * kordoc 이 없으면 건너뛴다 — 설치는 선택이고, 없으면 기능이 꺼질 뿐이다.
 */
describe('실물 kordoc 생성', () => {
  it('마크다운의 제목·표·목록이 hwpx 왕복에서 살아남는다', async () => {
    let kordoc: { markdownToHwpx: (md: string) => Promise<ArrayBuffer>; parse: (b: Uint8Array) => Promise<unknown> };
    try {
      kordoc = (await import('kordoc')) as never;
    } catch {
      return; // kordoc 미설치 — 건너뛴다
    }

    const dir = await tmpDir();
    const target = path.join(dir, `계약 요약${GENERATED_EXTENSION}`);
    const markdown =
      '# 자문 계약 요약\n\n## 제1조 목적\n본 계약은 자문 업무를 정한다.\n\n' +
      '| 항목 | 내용 |\n| --- | --- |\n| 기간 | 6개월 |\n\n- 첫째 항목\n';

    await writeHwpx({ targetPath: target, markdown });

    const parsed = await kordoc.parse(new Uint8Array(await fs.readFile(target)));
    const back = typeof parsed === 'string' ? parsed : (parsed as { markdown: string }).markdown;
    expect(back).toContain('자문 계약 요약');
    expect(back).toContain('6개월'); // 표 셀
    expect(back).toContain('첫째 항목'); // 목록
  }, 300_000);
});
