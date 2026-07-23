import { describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import {
  fillTemplate,
  isTemplate,
  readTemplate,
  selectHwpTemplate,
} from '@main/kordoc/template';

const FIXTURE = '/Users/jeong-uchang/Downloads/[MD포함]_[서연]업무기록/일일업무.hwp';

describe('read-only HWP template support', () => {
  it('recognizes only HWP and HWPX', () => {
    expect(isTemplate('/a/일일업무.hwp')).toBe(true);
    expect(isTemplate('/a/보고서.HWPX')).toBe(true);
    expect(isTemplate('/a/메모.md')).toBe(false);
    expect(isTemplate('/a/보고서.docx')).toBe(false);
  });

  it('disables the direct desktop template writer', async () => {
    await expect(
      fillTemplate({
        templatePath: '/a/일일업무.hwp',
        filledMarkdown: '내용',
        targetPath: '/a/사본.hwp',
      }),
    ).rejects.toThrow(/API v2/);
  });

  it('keeps optional real fixtures read-only', async () => {
    const exists = await fs.access(FIXTURE).then(
      () => true,
      () => false,
    );
    if (!exists) return;
    const before = await fs.readFile(FIXTURE);
    expect(await readTemplate(FIXTURE)).toContain('일일');
    expect(await fs.readFile(FIXTURE)).toEqual(before);
  }, 300_000);
});

describe('indexed HWP template selection', () => {
  const indexed = (relPath: string) => ({
    absPath: `C:\\workspace\\${relPath}`,
    relPath,
    status: 'done',
    deleted: false,
  });

  it('automatically selects the only indexed HWP template', () => {
    expect(selectHwpTemplate([indexed('일일업무.hwp')], '보고서를 작성해줘')?.relPath)
      .toBe('일일업무.hwp');
  });

  it('selects the uniquely matching HWP template from the user query', () => {
    const files = [indexed('양식/계약서.hwp'), indexed('양식/일일업무.hwp')];
    expect(selectHwpTemplate(files, '일일업무 양식대로 보고서를 작성해줘')?.relPath)
      .toBe('양식/일일업무.hwp');
  });

  it('does not guess when multiple templates are ambiguous', () => {
    const files = [indexed('양식/계약서.hwp'), indexed('양식/일일업무.hwp')];
    expect(selectHwpTemplate(files, '양식으로 문서를 만들어줘')).toBeNull();
  });

  it('ignores failed, deleted, and HWPX candidates', () => {
    expect(selectHwpTemplate([
      { ...indexed('실패.hwp'), status: 'failed' },
      { ...indexed('삭제.hwp'), deleted: true },
      indexed('기존.hwpx'),
    ], '기존 양식')).toBeNull();
  });
});
