import { describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import { fillTemplate, isTemplate, readTemplate } from '@main/kordoc/template';

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
