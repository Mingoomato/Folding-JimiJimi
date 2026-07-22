import { describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fillTemplate, isTemplate, readTemplate } from '@main/kordoc/template';

/**
 * 양식 복사·채우기.
 *
 * 이 경로에서 가장 중요한 약속은 **원본 양식을 건드리지 않는 것**이다. 일일업무일지 같은
 * 문서는 빈 양식을 두고 매번 새 사본을 채운다. 원본을 덮으면 다음에 쓸 양식이 사라진다.
 */
const FIXTURE = '/Users/jeong-uchang/Downloads/[MD포함]_[시연]업무기록/일일업무.hwp';

async function tmpDir(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'folding-tpl-'));
}

async function hasFixture(): Promise<boolean> {
  return fs
    .access(FIXTURE)
    .then(() => true)
    .catch(() => false);
}

describe('isTemplate', () => {
  it('한글 양식만 받는다', () => {
    expect(isTemplate('/a/일일업무.hwp')).toBe(true);
    expect(isTemplate('/a/보고서.HWPX')).toBe(true);
    expect(isTemplate('/a/메모.md')).toBe(false);
    expect(isTemplate('/a/보고서.docx')).toBe(false);
  });
});

describe('fillTemplate 안전장치', () => {
  it('원본 양식을 덮어쓰려 하면 시작조차 하지 않는다', async () => {
    await expect(
      fillTemplate({
        templatePath: '/a/일일업무.hwp',
        filledMarkdown: '내용',
        targetPath: '/a/일일업무.hwp',
      }),
    ).rejects.toThrow(/원본 양식은 덮어쓸 수 없습니다/);
  });

  it('빈 내용으로는 사본을 만들지 않는다', async () => {
    await expect(
      fillTemplate({
        templatePath: '/a/일일업무.hwp',
        filledMarkdown: '   ',
        targetPath: '/a/사본.hwp',
      }),
    ).rejects.toThrow(/비어 있습니다/);
  });

  it('원본과 다른 형식으로는 저장하지 않는다 — hwp 는 hwp 로', async () => {
    await expect(
      fillTemplate({
        templatePath: '/a/일일업무.hwp',
        filledMarkdown: '내용',
        targetPath: '/a/사본.hwpx',
      }),
    ).rejects.toThrow(/같은 형식으로만/);
  });
});

/**
 * 실물 양식으로 도는 검증. 양식 파일과 kordoc 이 있어야 돈다.
 * 이 테스트가 지키는 것: 사본은 채워지고, **원본은 바이트 하나 바뀌지 않는다.**
 */
describe('실물 양식 복사·채우기', () => {
  it('사본에만 채워지고 원본은 그대로다', async () => {
    if (!(await hasFixture())) return; // 양식 없으면 건너뛴다

    const before = await fs.readFile(FIXTURE);
    const markdown = await readTemplate(FIXTURE);
    expect(markdown).toContain('일일');

    const anchor = ['송용휘', 'CODEGATE-001', '개발부서'].find((word) => markdown.includes(word));
    expect(anchor, '양식에서 채울 자리를 찾지 못했다').toBeDefined();

    const dir = await tmpDir();
    const target = path.join(dir, '일일업무_작성본.hwp');
    await fillTemplate({
      templatePath: FIXTURE,
      filledMarkdown: markdown.replace(anchor!, '채움확인값'),
      targetPath: target,
    });

    // 사본에 반영됐는가
    expect(await readTemplate(target)).toContain('채움확인값');
    // 원본은 바이트 하나 안 바뀌었는가 — 이게 이 기능의 존재 이유다
    expect(await fs.readFile(FIXTURE)).toEqual(before);
  }, 600_000);
});
