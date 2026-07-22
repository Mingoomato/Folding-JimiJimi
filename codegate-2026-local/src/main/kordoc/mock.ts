/**
 * 픽스처 기반 kordoc 목 구현 (`CODEGATE_MOCK=1`).
 * 실물 kordoc 이 붙기 전까지 M1~M3 데모를 굴리기 위한 것이며, 원본 안전 규칙
 * (백업 → 임시파일 → rename)은 `KordocBase` 를 그대로 타므로 실물과 동일하게 검증된다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';
import { KordocBase, type RunPatchOutcome } from './base';

/**
 * 목에서 "부분 실패(exit 2)"를 재현하는 표식.
 * 편집 마크다운에 이 줄이 들어 있으면 해당 편집은 미적용으로 보고된다.
 */
export const MOCK_UNAPPLIED_MARK = '<!-- kordoc:unapplied -->';

/** 1x1 투명 PNG — 렌더 결과 자리표시자. */
const TRANSPARENT_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

export interface MockKordocOptions {
  /**
   * `<fixtureDir>/<파일명>.md` 가 있으면 parse 결과로 사용한다.
   * 없으면 파일명 기반으로 그럴듯한 한국어 마크다운을 생성한다.
   */
  fixtureDir?: string;
}

export class MockKordoc extends KordocBase {
  private readonly fixtureDir?: string;

  constructor(options: MockKordocOptions = {}) {
    super();
    this.fixtureDir = options.fixtureDir;
  }

  async parse(filePath: string): Promise<string> {
    const name = path.basename(filePath);
    if (this.fixtureDir) {
      const fixture = path.join(this.fixtureDir, `${name}.md`);
      try {
        return await fs.readFile(fixture, 'utf8');
      } catch {
        // 픽스처가 없으면 생성 폴백
      }
    }
    // 텍스트 계열은 원본을 그대로 마크다운으로 취급한다
    const ext = path.extname(filePath).toLowerCase();
    if (ext === '.md' || ext === '.txt') {
      return fs.readFile(filePath, 'utf8');
    }
    const stem = path.basename(filePath, path.extname(filePath));
    return [
      `# ${stem}`,
      '',
      '> 이 문서는 목(mock) kordoc 이 생성한 자리표시자입니다.',
      '',
      '## 1. 개요',
      '',
      `${stem} 문서의 본문이 여기에 들어갑니다.`,
      '',
      '## 2. 항목',
      '',
      '| 항목 | 내용 |',
      '| --- | --- |',
      '| 작성일 | 2026-07-21 |',
      '| 상태 | 초안 |',
      '',
    ].join('\n');
  }

  async generate(md: string, outPath: string, preset?: string): Promise<void> {
    await fs.mkdir(path.dirname(outPath), { recursive: true });
    const header = preset ? `<!-- preset: ${preset} -->\n` : '';
    await fs.writeFile(outPath, `${header}${md}`, 'utf8');
  }

  protected async runPatch(
    _sourcePath: string,
    editedMd: string,
    outPath: string,
  ): Promise<RunPatchOutcome> {
    const lines = editedMd.split('\n');
    const unapplied = lines
      .filter((line) => line.includes(MOCK_UNAPPLIED_MARK))
      .map((line) => line.replace(MOCK_UNAPPLIED_MARK, '').trim() || '이름 없는 편집');

    const applied = Math.max(lines.filter((l) => l.trim().length > 0).length - unapplied.length, 0);
    const body = lines.filter((line) => !line.includes(MOCK_UNAPPLIED_MARK)).join('\n');

    await fs.writeFile(outPath, body, 'utf8');
    return { exitCode: unapplied.length > 0 ? 2 : 0, applied, unapplied };
  }

  protected async renderPages(filePath: string, pages?: number[]): Promise<Buffer[]> {
    const count = pages && pages.length > 0 ? pages.length : 1;
    if (count > 50) {
      throw new UserFacingError(`한 번에 렌더할 수 있는 페이지는 50장까지입니다 (${filePath}).`);
    }
    return Array.from({ length: count }, () => Buffer.from(TRANSPARENT_PNG));
  }
}
