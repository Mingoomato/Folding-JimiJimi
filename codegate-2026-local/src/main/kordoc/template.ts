/**
 * 양식을 **복사해서 채운다.**
 *
 * 일일업무일지 같은 문서는 빈 양식을 두고 매번 새 사본을 채운다. 원본 양식을 고쳐 버리면
 * 다음에 쓸 빈 양식이 사라진다. 그래서 이 경로는 **원본을 절대 건드리지 않고** 채운
 * 사본만 새로 만든다.
 *
 * 어떻게 서식이 보존되는가
 * ----------------------
 *   원본 .hwp ──parse──▶ 마크다운(빈 칸 포함)
 *   마크다운 채움 ──patchHwp(원본, 채운 마크다운)──▶ 새 .hwp
 *
 * `markdownToHwpx` 로 새로 만드는 것과 다르다. 그건 빈 문서에서 시작하니 양식의 표
 * 레이아웃·글꼴·칸 너비가 사라지고 확장자도 .hwpx 가 된다. patch 는 **원본 바이트를
 * 바탕으로** 텍스트만 갈아끼우므로 양식 그대로에 .hwp 를 유지한다.
 *
 * 못 채우는 자리
 * -------------
 * 표 셀과 본문 문단은 채워진다. 머리말·꼬리말·개체가 걸린 자리는 kordoc 이 `skipped` 로
 * 보고하는데, 그때는 **일부만 반영된 사본을 남기지 않고 실패로 올린다.** 채운 줄 알았는데
 * 비어 있는 보고서가 나가는 것이 가장 나쁘다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';

/** 이 경로가 다루는 확장자. */
export const TEMPLATE_EXTENSIONS = ['.hwp', '.hwpx'] as const;

export function isTemplate(filePath: string): boolean {
  return (TEMPLATE_EXTENSIONS as readonly string[]).includes(path.extname(filePath).toLowerCase());
}

interface KordocApi {
  parse(bytes: Uint8Array): Promise<string | { markdown: string }>;
  patchHwp(original: Uint8Array, markdown: string): Promise<PatchResult>;
  patchHwpx(original: Uint8Array, markdown: string): Promise<PatchResult>;
}

interface PatchResult {
  success?: boolean;
  data?: ArrayBuffer | Uint8Array;
  skipped?: unknown[];
}

async function loadKordoc(): Promise<KordocApi> {
  try {
    return (await import('kordoc')) as unknown as KordocApi;
  } catch (error) {
    throw new UserFacingError(
      '한글 양식을 다룰 수 없습니다. kordoc 이 설치되어 있는지 확인해 주세요.',
      { cause: error },
    );
  }
}

/** 양식을 마크다운으로 연다 — 이걸 채워서 다시 넣는다. */
export async function readTemplate(templatePath: string): Promise<string> {
  if (!isTemplate(templatePath)) {
    throw new UserFacingError('한글 양식(.hwp, .hwpx)만 열 수 있습니다.');
  }
  const kordoc = await loadKordoc();
  const parsed = await kordoc.parse(new Uint8Array(await fs.readFile(templatePath)));
  const markdown = typeof parsed === 'string' ? parsed : parsed.markdown;
  if (!markdown?.trim()) {
    throw new UserFacingError('양식에서 내용을 읽지 못했습니다.');
  }
  return markdown;
}

export interface FillTemplateOptions {
  templatePath: string;
  /** 양식을 읽어 채운 마크다운. 원본과 **같은 구조**여야 채워진다. */
  filledMarkdown: string;
  /** 채운 사본을 저장할 경로. 원본과 같으면 거부한다. */
  targetPath: string;
}

/**
 * 양식을 채운 **새 파일**을 만든다. 원본은 읽기만 한다.
 */
export async function fillTemplate({
  templatePath,
  filledMarkdown,
  targetPath,
}: FillTemplateOptions): Promise<void> {
  if (!filledMarkdown.trim()) {
    throw new UserFacingError('채울 내용이 비어 있습니다.');
  }
  // 원본을 덮으면 다음에 쓸 빈 양식이 사라진다. 경로가 같으면 시작도 하지 않는다.
  if (path.resolve(templatePath) === path.resolve(targetPath)) {
    throw new UserFacingError('원본 양식은 덮어쓸 수 없습니다. 다른 이름으로 저장해 주세요.');
  }
  if (path.extname(targetPath).toLowerCase() !== path.extname(templatePath).toLowerCase()) {
    throw new UserFacingError('채운 사본은 원본 양식과 같은 형식으로만 저장할 수 있습니다.');
  }

  const kordoc = await loadKordoc();
  const original = new Uint8Array(await fs.readFile(templatePath));
  const patch =
    path.extname(templatePath).toLowerCase() === '.hwpx' ? kordoc.patchHwpx : kordoc.patchHwp;
  const result = await patch(original, filledMarkdown);

  const skipped = result.skipped ?? [];
  if (skipped.length > 0) {
    // 일부만 채운 사본을 남기지 않는다 — 채운 줄 알았는데 빈 보고서가 나가는 게 가장 나쁘다.
    throw new UserFacingError(
      `양식의 ${skipped.length}곳을 채우지 못했습니다. 머리말·개체가 걸린 자리는 한글에서 직접 채워 주세요.`,
    );
  }
  const bytes = result.data ? new Uint8Array(result.data as ArrayBuffer) : null;
  if (!result.success || !bytes?.byteLength) {
    throw new UserFacingError('양식을 채우지 못했습니다.');
  }
  // `wx` — 그 사이 누가 같은 이름을 만들었으면 덮지 않고 실패한다.
  await fs.writeFile(targetPath, bytes, { flag: 'wx' });
}
