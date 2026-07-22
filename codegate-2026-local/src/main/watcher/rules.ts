/**
 * 감시 대상 판별 규칙 (스펙 v1.3 §1 L2 · §3 스캔 미리보기 "261 포함 / 163 제외").
 * 순수 함수만 둔다 — 스캔 미리보기와 chokidar 필터가 같은 규칙을 공유해야 하기 때문.
 */
import path from 'node:path';

/** 위키 빌드가 처리할 수 있는 확장자. 이 밖은 전부 제외된다. */
export const SUPPORTED_EXTENSIONS = [
  '.hwp',
  '.hwpx',
  '.doc',
  '.docx',
  '.pdf',
  '.pptx',
  '.xlsx',
  '.md',
  '.txt',
] as const;

/** 통째로 건너뛰는 디렉터리 이름. */
export const SKIPPED_DIRS = new Set([
  'node_modules',
  '.git',
  '.svn',
  '__pycache__',
  'dist',
  'build',
  'out',
  '.codegate',
]);

export type ExcludeReason = string;

export interface Classification {
  included: boolean;
  /** 제외된 이유 — 사용자에게 그대로 보여준다 (스펙 v1.3 §3). */
  reason?: ExcludeReason;
}

/** 숨김 파일/디렉터리 (`.` 로 시작). */
export function isHidden(name: string): boolean {
  return name.startsWith('.');
}

/** 오피스 잠금 파일 (`~$계약서.hwp`) 및 임시 저장물. */
export function isLockFile(name: string): boolean {
  return name.startsWith('~$') || name.startsWith('~') || name.endsWith('.tmp');
}

export function isSkippedDir(name: string): boolean {
  return SKIPPED_DIRS.has(name) || isHidden(name);
}

/**
 * 등록 폴더 기준 상대 경로 하나를 분류한다.
 * `relPath` 는 POSIX 구분자든 OS 구분자든 상관없다.
 */
export function classifyFile(relPath: string): Classification {
  const segments = relPath.split(/[\\/]/).filter(Boolean);
  const name = segments[segments.length - 1] ?? '';

  for (const dir of segments.slice(0, -1)) {
    if (SKIPPED_DIRS.has(dir)) return { included: false, reason: `제외 폴더(${dir}) 안의 파일` };
    if (isHidden(dir)) return { included: false, reason: '숨김 폴더 안의 파일' };
  }

  if (isHidden(name)) return { included: false, reason: '숨김 파일' };
  if (isLockFile(name)) return { included: false, reason: '오피스 잠금·임시 파일' };

  const ext = path.extname(name).toLowerCase();
  if (!ext) return { included: false, reason: '확장자 없음' };
  if (!(SUPPORTED_EXTENSIONS as readonly string[]).includes(ext)) {
    return { included: false, reason: `지원하지 않는 확장자(${ext})` };
  }
  return { included: true };
}

/** chokidar `ignored` 콜백 — 디렉터리/파일 모두 이 함수로 걸러진다. */
export function makeChokidarIgnore(rootPath: string) {
  return (target: string, stats?: { isDirectory(): boolean }): boolean => {
    const rel = path.relative(rootPath, target);
    if (rel === '') return false; // 루트 자신
    const name = path.basename(target);
    if (stats?.isDirectory()) return isSkippedDir(name);
    // 디렉터리인지 아직 모를 때: 확장자가 없으면 디렉터리로 보고 통과시킨다
    if (!stats && !path.extname(name)) return isSkippedDir(name);
    return !classifyFile(rel).included;
  };
}
