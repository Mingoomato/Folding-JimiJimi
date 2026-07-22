import fs from 'node:fs/promises';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';

export const TEMPLATE_EXTENSIONS = ['.hwp', '.hwpx'] as const;

export function isTemplate(filePath: string): boolean {
  return (TEMPLATE_EXTENSIONS as readonly string[]).includes(path.extname(filePath).toLowerCase());
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
