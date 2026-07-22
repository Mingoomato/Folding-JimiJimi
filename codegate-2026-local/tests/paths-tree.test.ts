/**
 * 가상 경로·포함/제외 규칙·트리 조립 — L1 트리와 `IPC.openOriginal` 이 같은 규약을 공유하는지 확인한다.
 */
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Root } from '@contracts';
import type { FileRow } from '@main/db/store';
import { buildTree } from '@main/tree';
import {
  isInside,
  makeRootId,
  sourceUriToRelativePath,
  splitVirtualPath,
  toVirtualPath,
} from '@main/util/vpath';
import { classifyFile } from '@main/watcher/rules';

function root(dirPath: string): Root {
  return {
    id: makeRootId(dirPath),
    path: dirPath,
    includedCount: 0,
    excludedCount: 0,
    addedAt: '2026-07-21T00:00:00.000Z',
  };
}

function file(rootId: string, relPath: string, status: FileRow['status'] = 'done'): FileRow {
  return {
    rootId,
    relPath,
    absPath: `/tmp/${relPath}`,
    sha256: 'x',
    size: 1,
    mtime: '',
    status,
    dirty: false,
    deleted: false,
  };
}

describe('가상 경로', () => {
  it('rootId 는 폴더 경로에서 결정되어 재등록해도 같다', () => {
    expect(makeRootId('/Users/demo/문서')).toBe(makeRootId('/Users/demo/문서/'));
  });

  it('왕복 변환이 보존된다', () => {
    const id = makeRootId('/Users/demo/문서');
    const vpath = toVirtualPath(id, '계약/2026/계약서.hwpx');
    expect(splitVirtualPath(vpath)).toEqual({ rootId: id, relPath: '계약/2026/계약서.hwpx' });
  });

  it('등록 폴더 밖 경로를 걸러낸다', () => {
    expect(isInside('/Users/demo/문서', '/Users/demo/문서/a.hwpx')).toBe(true);
    expect(isInside('/Users/demo/문서', path.join('/Users/demo/문서', '../비밀.hwpx'))).toBe(false);
  });

  it('canonical source URI를 Unicode·예약문자 원본 경로로 되돌린다', () => {
    expect(
      sourceUriToRelativePath(
        'source://%EA%B7%9C%EC%A0%95/100%25%20%231%3F.md',
      ),
    ).toBe('규정/100% #1?.md');
    expect(sourceUriToRelativePath('source://docs/%2E%2E/secret.md')).toBeNull();
  });
});

describe('포함/제외 규칙', () => {
  it('지원 확장자만 포함한다', () => {
    expect(classifyFile('계약서.hwpx').included).toBe(true);
    expect(classifyFile('보고서.docx').included).toBe(true);
    expect(classifyFile('사진.png').included).toBe(false);
  });

  it('숨김·잠금·제외 폴더는 걸러낸다', () => {
    expect(classifyFile('.DS_Store').included).toBe(false);
    expect(classifyFile('~$계약서.hwp').reason).toContain('잠금');
    expect(classifyFile('node_modules/pkg/readme.md').included).toBe(false);
    expect(classifyFile('.git/config.txt').included).toBe(false);
  });
});

describe('트리 조립', () => {
  it('폴더를 접고 상위 상태를 자식 중 가장 시급한 값으로 올린다', () => {
    const r = root('/Users/demo/문서');
    const tree = buildTree(r ? [r] : [], [
      file(r.id, '계약/계약서.hwpx', 'done'),
      file(r.id, '계약/부속합의서.hwpx', 'converting'),
      file(r.id, '안내문.pdf', 'done'),
    ]);

    expect(tree).toHaveLength(1);
    expect(tree[0]!.path).toBe(r.id);
    expect(tree[0]!.status).toBe('converting');

    const dir = tree[0]!.children!.find((c) => c.isDir)!;
    expect(dir.name).toBe('계약');
    expect(dir.status).toBe('converting');
    expect(dir.children!.map((c) => c.path)).toContain(toVirtualPath(r.id, '계약/계약서.hwpx'));

    // 폴더가 파일보다 먼저 온다
    expect(tree[0]!.children![0]!.isDir).toBe(true);
  });
});
