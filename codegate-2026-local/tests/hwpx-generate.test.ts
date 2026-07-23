import { describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
  safeFileName,
  titleFromMarkdown,
  titleFromUserQuery,
  uniquePath,
  writeHwpx,
} from '@main/kordoc/generate';

async function tmpDir(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'folding-gen-'));
}

describe('safeFileName', () => {
  it('filters path syntax', () => {
    expect(safeFileName('../../etc/passwd')).not.toContain('/');
    expect(safeFileName('보고서: 1/2 <초안>')).toBe('보고서 1 2 초안');
  });

  it('uses the fallback for empty or dot-only names', () => {
    expect(safeFileName('///')).toBe('문서');
    expect(safeFileName('..')).toBe('문서');
    expect(safeFileName('   ')).toBe('문서');
  });

  it('limits file name length', () => {
    expect(safeFileName('가'.repeat(300)).length).toBeLessThanOrEqual(80);
  });
});

describe('titleFromMarkdown', () => {
  it('uses the first heading', () => {
    expect(titleFromMarkdown('# 자문 계약 요약\n\n본문')).toBe('자문 계약 요약');
    expect(titleFromMarkdown('본문 먼저\n\n## 후속 제목')).toBe('본문 먼저');
  });

  it('strips leading list syntax from a first sentence', () => {
    expect(titleFromMarkdown('- 첫째 항목\n- 둘째')).toBe('첫째 항목');
  });

  it('uses the fallback for empty Markdown', () => {
    expect(titleFromMarkdown('   \n\n')).toBe('문서');
  });
});

describe('titleFromUserQuery', () => {
  it('uses an explicit file name request', () => {
    expect(titleFromUserQuery('파일 이름은 "7월 업무 보고서"로 저장해줘', '# 다른 제목'))
      .toBe('7월 업무 보고서');
  });

  it('derives a safe suggested name from the request instead of the answer heading', () => {
    expect(
      titleFromUserQuery(
        '일일업무 양식대로 업무 내용 모두 정리해서 보고서 작성해줘',
        '# 업무 보고서',
      ),
    ).toBe('일일업무 보고서');
  });

  it('falls back to the Markdown title when the query is absent', () => {
    expect(titleFromUserQuery(undefined, '# 업무 보고서')).toBe('업무 보고서');
  });
});

describe('uniquePath', () => {
  it('adds a numeric suffix without writing a file', async () => {
    const dir = await tmpDir();
    await fs.writeFile(path.join(dir, '보고서.hwpx'), 'x');
    expect(path.basename(await uniquePath(dir, '보고서', '.hwpx'))).toBe('보고서 (2).hwpx');
  });
});

describe('desktop HWPX writer boundary', () => {
  it('requires the API v2 approval flow', async () => {
    await expect(
      writeHwpx({ targetPath: 'report.hwpx', markdown: '# Report' }),
    ).rejects.toThrow(/API v2/);
  });
});

describe('real Kordoc artifact round trip', () => {
  it('preserves headings, tables, and lists without a desktop filesystem write', async () => {
    let kordoc: {
      markdownToHwpx: (markdown: string) => Promise<ArrayBuffer>;
      parse: (content: Uint8Array) => Promise<unknown>;
    };
    try {
      kordoc = (await import('kordoc')) as never;
    } catch {
      return;
    }
    const markdown =
      '# 자문 계약 요약\n\n## 제1조 목적\n본 계약은 자문 업무를 정한다.\n\n' +
      '| 항목 | 내용 |\n| --- | --- |\n| 기간 | 6개월 |\n\n- 첫째 항목\n';
    const artifact = new Uint8Array(await kordoc.markdownToHwpx(markdown));
    const parsed = await kordoc.parse(artifact);
    const back = typeof parsed === 'string' ? parsed : (parsed as { markdown: string }).markdown;
    expect(back).toContain('자문 계약 요약');
    expect(back).toContain('6개월');
    expect(back).toContain('첫째 항목');
  }, 300_000);
});
