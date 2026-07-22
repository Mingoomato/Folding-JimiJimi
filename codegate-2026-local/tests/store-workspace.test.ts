import { describe, expect, it } from 'vitest';
import type { Root } from '@contracts';
import { Store, type Db, type FileRow } from '@main/db/store';

describe('workspace snapshot transaction', () => {
  it('파일 snapshot 저장이 실패하면 root insert도 rollback한다', () => {
    const store = new Store(fakeDb());
    const root: Root = {
      id: 'root-a',
      path: '/workspace/a',
      includedCount: 1,
      excludedCount: 0,
      addedAt: '2026-07-22T00:00:00.000Z',
    };
    const mismatched: FileRow = {
      rootId: 'root-b',
      relPath: 'manual.md',
      absPath: '/workspace/a/manual.md',
      sha256: 'a'.repeat(64),
      size: 1,
      mtime: '2026-07-22T00:00:00.000Z',
      status: 'done',
      dirty: false,
      deleted: false,
    };

    try {
      expect(() => store.replaceRootFiles(root, [mismatched])).toThrow('다른 rootId');
      expect(store.listRoots()).toEqual([]);
      expect(store.listFiles()).toEqual([]);
    } finally {
      store.close();
    }
  });

  it('기존 파일을 새 snapshot으로 원자적으로 교체한다', () => {
    const store = new Store(fakeDb());
    const root: Root = {
      id: 'root-a',
      path: '/workspace/a',
      includedCount: 1,
      excludedCount: 0,
      addedAt: '2026-07-22T00:00:00.000Z',
    };
    const row = (relPath: string): FileRow => ({
      rootId: root.id,
      relPath,
      absPath: `/workspace/a/${relPath}`,
      sha256: 'b'.repeat(64),
      size: 1,
      mtime: '2026-07-22T00:00:00.000Z',
      status: 'done',
      dirty: false,
      deleted: false,
    });

    try {
      store.replaceRootFiles(root, [row('old.md')]);
      store.replaceRootFiles(root, [row('new.md')]);
      expect(store.listFiles().map((file) => file.relPath)).toEqual(['new.md']);
    } finally {
      store.close();
    }
  });
});

function fakeDb(): Db {
  let roots: Array<Record<string, unknown>> = [];
  let files: Array<Record<string, unknown>> = [];
  const db = {
    prepare(sql: string) {
      return {
        run(params?: Record<string, unknown> | string) {
          if (sql.includes('INSERT INTO roots')) {
            const input = params as Record<string, unknown>;
            roots = roots.filter((root) => root.id !== input.id);
            roots.push({
              id: input.id,
              path: input.path,
              included_count: input.included,
              excluded_count: input.excluded,
              added_at: input.addedAt,
            });
          } else if (sql.includes('DELETE FROM files')) {
            files = files.filter((file) => file.root_id !== params);
          } else if (sql.includes('INSERT INTO files')) {
            const input = params as Record<string, unknown>;
            files.push({
              root_id: input.rootId,
              rel_path: input.relPath,
              abs_path: input.absPath,
              sha256: input.sha256,
              size: input.size,
              mtime: input.mtime,
              status: input.status,
              dirty: input.dirty,
              deleted: input.deleted,
            });
          }
        },
        all() {
          if (sql.includes('FROM roots')) return roots;
          if (sql.includes('FROM files')) return files;
          return [];
        },
      };
    },
    transaction<TArgs extends unknown[]>(fn: (...args: TArgs) => void) {
      return (...args: TArgs) => {
        const previousRoots = roots.map((root) => ({ ...root }));
        const previousFiles = files.map((file) => ({ ...file }));
        try {
          fn(...args);
        } catch (error) {
          roots = previousRoots;
          files = previousFiles;
          throw error;
        }
      };
    },
    close() {},
  };
  return db as unknown as Db;
}
