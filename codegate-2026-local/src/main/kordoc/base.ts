/**
 * L3 kordoc 래퍼 공통 골격 (스펙 v1.3 §1 L3).
 *
 * 계약(`KordocApi`)의 `patch` 는 구현체가 무엇이든 반드시
 * "백업 → 임시파일 → rename" 순서를 지나야 하므로, 그 순서를 여기 한 곳에 고정한다.
 * 구현체는 `runPatch`(문서 변환)만 책임진다.
 *
 * 금지: `fill` — 오탐이 많아 기획안 §8 에서 사용이 막혀 있다. patch + 명시 치환만 쓴다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import type { KordocApi, PatchResult } from '@contracts';
import { UserFacingError } from '@main/util/errors';
import { atomicReplaceFile, type AtomicReplaceHooks } from './atomic';

/** 구현체가 돌려주는 kordoc 실행 결과. */
export interface RunPatchOutcome {
  /** kordoc 프로세스 종료 코드. 0=성공, 2=부분 실패(미적용 편집 있음). */
  exitCode: number;
  applied: number;
  unapplied: string[];
}

/** hwpx 만 렌더 가능 — `.hwp` 는 계약상 지원하지 않는다. */
export const RENDERABLE_EXTENSIONS = ['.hwpx'];

export abstract class KordocBase implements KordocApi {
  abstract parse(filePath: string): Promise<string>;
  abstract generate(md: string, outPath: string, preset?: string): Promise<void>;

  /** 편집된 마크다운을 `outPath` 에 새 문서로 만들어 놓는다. 원본은 건드리지 않는다. */
  protected abstract runPatch(
    sourcePath: string,
    editedMd: string,
    outPath: string,
  ): Promise<RunPatchOutcome>;

  /** hwpx 페이지 이미지 렌더 (확장자 검사는 상위에서 이미 끝난 상태). */
  protected abstract renderPages(filePath: string, pages?: number[]): Promise<Buffer[]>;

  /**
   * 스펙 v1.3 §5 "원본 안전".
   * ① `.bak` 백업 → ② 임시파일 → ③ rename. exit 2 는 절대 성공으로 취급하지 않는다.
   */
  async patch(filePath: string, editedMd: string, hooks?: AtomicReplaceHooks): Promise<PatchResult> {
    await assertExistingFile(filePath);

    let outcome: RunPatchOutcome | undefined;
    const { backupPath } = await atomicReplaceFile(
      filePath,
      async (tempPath) => {
        outcome = await this.runPatch(filePath, editedMd, tempPath);

        if (outcome.exitCode !== 0 && outcome.exitCode !== 2) {
          throw new UserFacingError(
            `문서를 수정하지 못했습니다 (kordoc 종료 코드 ${outcome.exitCode}). 원본은 그대로 두었습니다.`,
          );
        }
        // exit 2 인데 적용된 편집이 하나도 없다면 덮어쓸 이유가 없다 → 원본 유지
        if (outcome.exitCode === 2 && outcome.applied === 0) {
          throw new UserFacingError(
            '요청한 편집을 하나도 적용하지 못했습니다. 원본은 그대로 두었습니다.',
          );
        }
      },
      hooks ?? {},
    );

    if (!outcome) {
      throw new UserFacingError('문서 수정 결과를 확인하지 못했습니다. 다시 시도해 주세요.');
    }

    return {
      // exit 2 = 부분 실패. 성공으로 삼키지 말 것 (스펙 v1.3 §5).
      ok: outcome.exitCode === 0,
      exitCode: outcome.exitCode,
      applied: outcome.applied,
      unapplied: outcome.unapplied,
      backupPath,
    };
  }

  /** `.hwp` 는 렌더할 수 없다 — md 표 구조로 판단해야 한다 (기획안 §8). */
  async render(filePath: string, pages?: number[]): Promise<Buffer[]> {
    const ext = path.extname(filePath).toLowerCase();
    if (ext === '.hwp') {
      throw new UserFacingError(
        '`.hwp` 문서는 페이지 이미지로 렌더할 수 없습니다. hwpx 로 저장한 뒤 다시 시도해 주세요.',
      );
    }
    if (!RENDERABLE_EXTENSIONS.includes(ext)) {
      throw new UserFacingError(
        `${ext || '이 형식'} 문서는 페이지 렌더를 지원하지 않습니다. hwpx 문서만 렌더할 수 있어요.`,
      );
    }
    await assertExistingFile(filePath);
    return this.renderPages(filePath, pages);
  }
}

export async function assertExistingFile(filePath: string): Promise<void> {
  try {
    const stat = await fs.stat(filePath);
    if (!stat.isFile()) throw new Error('not a file');
  } catch {
    throw new UserFacingError(`문서를 찾을 수 없습니다: ${path.basename(filePath)}`);
  }
}
