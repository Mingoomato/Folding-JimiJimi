/**
 * 가상 경로(virtual path) — 등록 폴더가 여러 개여도 충돌 없이 한 문자열로 파일을 가리키기 위한 규약.
 *
 *   가상 경로 = `<rootId>/<등록 폴더 기준 상대 경로>`
 *
 * 계약의 `FileNode.path` · `BuildManifest.files[].path` · `Citation.sourcePath` 가 모두 이 형식이며,
 * `IPC.openOriginal` 은 이 문자열을 다시 절대경로로 되돌린다.
 * rootId 는 폴더 절대경로에서 결정되므로 같은 폴더를 다시 등록해도 동일하다.
 */
import { createHash } from 'node:crypto';
import path from 'node:path';

/** 폴더 절대경로 → 안정적인 짧은 id (`계약서-3f9a1c` 형태). */
export function makeRootId(absPath: string): string {
  const digest = createHash('sha256').update(path.resolve(absPath)).digest('hex').slice(0, 6);
  const base = path.basename(path.resolve(absPath)).replace(/[^\p{L}\p{N}._-]/gu, '_') || 'root';
  return `${base}-${digest}`;
}

export function toVirtualPath(rootId: string, relPath: string): string {
  return relPath ? `${rootId}/${toPosix(relPath)}` : rootId;
}

export function splitVirtualPath(virtualPath: string): { rootId: string; relPath: string } {
  const normalized = toPosix(virtualPath).replace(/^\/+/, '');
  const slash = normalized.indexOf('/');
  if (slash < 0) return { rootId: normalized, relPath: '' };
  return { rootId: normalized.slice(0, slash), relPath: normalized.slice(slash + 1) };
}

/** 윈도우 구분자를 `/` 로 통일 — 가상 경로는 항상 POSIX 형식이다. */
export function toPosix(p: string): string {
  return p.split(path.sep).join('/');
}

/** canonical `source://` URI를 등록 폴더 기준 실제 상대경로로 되돌린다. */
export function sourceUriToRelativePath(value: string): string | null {
  if (!value.startsWith('source://')) return null;
  const encoded = value.slice('source://'.length);
  if (!encoded || /[?#]/.test(encoded)) return null;
  try {
    const relative = decodeURIComponent(encoded).replace(/^\/+/, '');
    const segments = relative.split('/');
    if (!relative || segments.some((segment) => !segment || segment === '.' || segment === '..')) {
      return null;
    }
    if (segments.map((segment) => encodeURIComponent(segment)).join('/') !== encoded) return null;
    return relative;
  } catch {
    return null;
  }
}

/** `child` 가 `parent` 안에 있는지 (심볼릭 링크 탈출·`..` 방지). */
export function isInside(parent: string, child: string): boolean {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
}
