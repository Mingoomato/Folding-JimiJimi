/**
 * 우측 디렉터리 트리 조립 (스펙 v1.3 §1 L1 — 파일별 상태 아이콘).
 * DB 의 평평한 파일 목록을 계약의 `FileNode[]` 로 접는다.
 */
import path from 'node:path';
import type { FileNode, FileStatus, Root } from '@contracts';
import type { FileRow } from '@main/db/store';
import { toVirtualPath } from '@main/util/vpath';

/** 상위 폴더 상태 = 자식들 중 가장 "시급한" 상태. */
const SEVERITY: Record<FileStatus, number> = { error: 3, converting: 2, pending: 1, done: 0 };

export function buildTree(roots: Root[], files: FileRow[]): FileNode[] {
  return roots.map((root) => {
    const rootNode: FileNode = {
      path: root.id,
      name: path.basename(root.path) || root.path,
      isDir: true,
      status: 'done',
      children: [],
    };

    for (const file of files.filter((f) => f.rootId === root.id)) {
      insert(rootNode, root.id, file);
    }

    sortTree(rootNode);
    rootNode.status = aggregate(rootNode);
    return rootNode;
  });
}

function insert(rootNode: FileNode, rootId: string, file: FileRow): void {
  const segments = file.relPath.split('/').filter(Boolean);
  let cursor = rootNode;
  let walked: string[] = [];

  for (const segment of segments.slice(0, -1)) {
    walked = [...walked, segment];
    const dirPath = toVirtualPath(rootId, walked.join('/'));
    let next = cursor.children?.find((c) => c.isDir && c.path === dirPath);
    if (!next) {
      next = { path: dirPath, name: segment, isDir: true, status: 'done', children: [] };
      cursor.children = [...(cursor.children ?? []), next];
    }
    cursor = next;
  }

  const name = segments[segments.length - 1] ?? file.relPath;
  cursor.children = [
    ...(cursor.children ?? []),
    {
      path: toVirtualPath(rootId, file.relPath),
      name,
      isDir: false,
      status: file.status,
    },
  ];
}

function aggregate(node: FileNode): FileStatus {
  if (!node.isDir || !node.children || node.children.length === 0) return node.status;
  let worst: FileStatus = 'done';
  for (const child of node.children) {
    const childStatus = child.isDir ? aggregate(child) : child.status;
    if (child.isDir) child.status = childStatus;
    if (SEVERITY[childStatus] > SEVERITY[worst]) worst = childStatus;
  }
  return worst;
}

/** 폴더 먼저, 그 다음 이름순 (한국어 로케일). */
function sortTree(node: FileNode): void {
  if (!node.children) return;
  node.children.sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
    return a.name.localeCompare(b.name, 'ko');
  });
  for (const child of node.children) sortTree(child);
}
