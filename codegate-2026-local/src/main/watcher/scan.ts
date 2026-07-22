/**
 * 폴더 스캔 — 등록 전 미리보기(스펙 v1.3 §3)와 등록 후 인덱싱에 함께 쓰인다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import type { ScanPreview } from '@contracts';
import type { FileRow } from '@main/db/store';
import { sha256File, walkFiles } from '@main/util/fsx';
import { toPosix } from '@main/util/vpath';
import { classifyFile, isSkippedDir } from './rules';

/** 포함/제외를 사용자에게 확인받기 위한 미리보기. */
export async function scanPreview(rootPath: string): Promise<ScanPreview> {
  const absPaths = await walkFiles(rootPath, { skipDir: isSkippedDir });
  const included: string[] = [];
  const excluded: { path: string; reason: string }[] = [];

  for (const abs of absPaths) {
    const rel = toPosix(path.relative(rootPath, abs));
    const verdict = classifyFile(rel);
    if (verdict.included) included.push(rel);
    else excluded.push({ path: rel, reason: verdict.reason ?? '제외 대상' });
  }
  return { root: rootPath, included, excluded };
}

/** 포함 대상 파일들의 해시·크기·mtime 을 계산해 DB 행으로 만든다. */
export async function indexRoot(rootId: string, rootPath: string): Promise<FileRow[]> {
  const preview = await scanPreview(rootPath);
  const rows: FileRow[] = [];
  for (const rel of preview.included) {
    const abs = path.join(rootPath, rel);
    const row = await describeFile(rootId, rel, abs);
    if (row) rows.push(row);
  }
  return rows;
}

/** 파일 하나를 DB 행으로. 읽을 수 없으면 null. */
export async function describeFile(
  rootId: string,
  relPath: string,
  absPath: string,
): Promise<FileRow | null> {
  try {
    const stat = await fs.stat(absPath);
    return {
      rootId,
      relPath: toPosix(relPath),
      absPath,
      sha256: await sha256File(absPath),
      size: stat.size,
      mtime: new Date(stat.mtimeMs).toISOString(),
      status: 'pending',
      dirty: true,
      deleted: false,
    };
  } catch {
    return null;
  }
}
