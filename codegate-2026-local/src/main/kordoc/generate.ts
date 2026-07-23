import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';

export const GENERATED_EXTENSION = '.hwpx';

export function safeFileName(title: string, fallback = '문서'): string {
  const cleaned = title
    .replace(/[\u0000-\u001f<>:"/\\|?*]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80);
  return cleaned && !/^\.+$/.test(cleaned) ? cleaned : fallback;
}

export function titleFromMarkdown(markdown: string): string {
  const firstLine = markdown.split('\n').find((line) => line.trim().length > 0);
  if (!firstLine) return safeFileName('');
  const heading = firstLine.match(/^\s{0,3}#{1,6}\s+(.+)$/);
  return safeFileName(heading ? heading[1] : firstLine.replace(/^[>\-*\s]+/, ''));
}

/** 저장 대화상자 기본 이름은 사용자 요청에서 만들고, 사용자가 최종 확정한다. */
export function titleFromUserQuery(userQuery: string | undefined, markdown: string): string {
  if (!userQuery?.trim()) return titleFromMarkdown(markdown);
  const explicit = userQuery.match(
    /(?:파일\s*이름|문서\s*이름|이름)\s*(?:은|을|를|:)?\s*["'「]?([^"'」\n]{2,80})["'」]?/,
  );
  const requested = explicit?.[1] ?? userQuery;
  const shortened = requested
    .replace(/\b(?:hwp|hwpx)\b/gi, ' ')
    .replace(/(?:양식대로|서식대로|문서로|파일로|업무\s*내용\s*모두|내용\s*모두)/g, ' ')
    .replace(
      /(?:작성|생성|저장|정리|만들)(?:해서|하여|해\s*줘|해주세요|해줘|어줘|어주세요|합니다|해)?/g,
      ' ',
    )
    .replace(/(?:부탁해|부탁합니다|줘|주세요)/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return safeFileName(shortened, titleFromMarkdown(markdown));
}

export async function uniquePath(dir: string, base: string, ext: string): Promise<string> {
  for (let n = 1; n < 1000; n += 1) {
    const name = n === 1 ? `${base}${ext}` : `${base} (${n})${ext}`;
    const candidate = path.join(dir, name);
    try {
      await fs.access(candidate);
    } catch {
      return candidate;
    }
  }
  throw new UserFacingError('같은 이름의 파일이 너무 많습니다. 이름을 바꿔 주세요.');
}

export type MarkdownToHwpx = (markdown: string) => Promise<ArrayBuffer>;

export interface WriteHwpxOptions {
  targetPath: string;
  markdown: string;
  convert?: MarkdownToHwpx;
}

export async function writeHwpx(_options: WriteHwpxOptions): Promise<never> {
  throw new UserFacingError(
    'Direct desktop HWPX writer is disabled. Use the API v2 capability, preview, and approval flow.',
  );
}
