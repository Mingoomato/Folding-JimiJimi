import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';

export const TEMPLATE_EXTENSIONS = ['.hwp', '.hwpx'] as const;

export interface IndexedTemplateCandidate {
  absPath: string;
  relPath: string;
  status: string;
  deleted: boolean;
}

const QUERY_STOP_WORDS = new Set([
  '문서', '파일', '양식', '서식', '내용', '모두', '기존', '복사본',
  '작성', '작성해줘', '작성해주세요', '만들어줘', '만들어주세요',
  '생성', '생성해줘', '생성해주세요', '저장', '저장해줘', '저장해주세요',
]);

export function isTemplate(filePath: string): boolean {
  return (TEMPLATE_EXTENSIONS as readonly string[]).includes(path.extname(filePath).toLowerCase());
}

function normalized(value: string): string {
  return value.normalize('NFKC').toLowerCase().replace(/[^0-9a-z가-힣]+/g, '');
}

function queryTokens(query: string): string[] {
  return query
    .normalize('NFKC')
    .toLowerCase()
    .split(/[^0-9a-z가-힣]+/)
    .filter((token) => token.length >= 2 && !QUERY_STOP_WORDS.has(token));
}

/** 인덱싱 완료된 HWP 중 질의와 파일명이 유일하게 가장 잘 맞는 양식을 고른다. */
export function selectHwpTemplate(
  files: readonly IndexedTemplateCandidate[],
  userQuery: string,
): IndexedTemplateCandidate | null {
  const eligible = files.filter((file) => (
    !file.deleted
    && file.status === 'done'
    && path.extname(file.absPath).toLowerCase() === '.hwp'
  ));
  if (eligible.length === 0) return null;
  if (eligible.length === 1) return eligible[0]!;

  const query = normalized(userQuery);
  const tokens = queryTokens(userQuery);
  const ranked = eligible.map((file) => {
    const stem = normalized(path.basename(file.relPath, path.extname(file.relPath)));
    const relative = normalized(file.relPath);
    let score = stem && query.includes(stem) ? 100 : 0;
    for (const token of tokens) {
      if (stem.includes(token) || token.includes(stem)) score += 20;
      else if (relative.includes(token)) score += 5;
    }
    if (/(양식|서식|template)/i.test(file.relPath)) score += 2;
    return { file, score };
  }).sort((left, right) => right.score - left.score || left.file.relPath.localeCompare(right.file.relPath));

  const best = ranked[0]!;
  const second = ranked[1];
  return best.score > 0 && best.score > (second?.score ?? -1) ? best.file : null;
}

interface KordocReadApi {
  parse(bytes: Uint8Array): Promise<string | { markdown: string }>;
}

async function loadKordoc(): Promise<KordocReadApi> {
  try {
    return (await import('kordoc')) as unknown as KordocReadApi;
  } catch (error) {
    throw new UserFacingError('한글 양식을 읽을 수 없습니다. kordoc 설치를 확인해 주세요.', {
      cause: error,
    });
  }
}

export async function readTemplate(templatePath: string): Promise<string> {
  if (!isTemplate(templatePath)) {
    throw new UserFacingError('한글 양식(.hwp, .hwpx)만 읽을 수 있습니다.');
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
  filledMarkdown: string;
  targetPath: string;
}

export async function fillTemplate(_options: FillTemplateOptions): Promise<never> {
  throw new UserFacingError(
    'Direct desktop template writer is disabled. Use an approved template_id through API v2.',
  );
}
