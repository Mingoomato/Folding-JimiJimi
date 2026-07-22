/**
 * SQLite 저장소 (스펙 v1.3 §1 L2 — 폴더·파일 상태·빌드 이력·대화·메시지).
 * better-sqlite3 는 동기 API 라서 메인 프로세스에서 그대로 쓴다 (트랜잭션이 단순해진다).
 */
import Database from 'better-sqlite3';
import type {
  ChatMessage,
  ChatRole,
  Citation,
  Conversation,
  ExecutionSummary,
  FileStatus,
  Root,
} from '@contracts';
import { MIGRATIONS } from './schema';

export type Db = Database.Database;

export interface FileRow {
  rootId: string;
  relPath: string;
  absPath: string;
  sha256: string;
  size: number;
  mtime: string;
  status: FileStatus;
  /** 마지막 빌드 이후 변경되었는가 (증분 업로드 대상) */
  dirty: boolean;
  deleted: boolean;
}

export interface BuildRow {
  id: string;
  status: string;
  progress: number;
  message: string;
  error: string | null;
  /** enrichment 보류 목록 JSON (스펙 v1.4 §5). 없으면 null. */
  deferred: string | null;
  createdAt: string;
  updatedAt: string;
}

interface RawFile {
  root_id: string;
  rel_path: string;
  abs_path: string;
  sha256: string;
  size: number;
  mtime: string;
  status: string;
  dirty: number;
  deleted: number;
}

/** 파일 경로를 받아 열고, 스키마를 최신으로 맞춘다 (멱등). */
export function openDb(file: string): Db {
  const db = new Database(file);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  migrate(db);
  return db;
}

export function migrate(db: Db): void {
  const current = Number((db.pragma('user_version', { simple: true }) as number) ?? 0);
  for (let version = current; version < MIGRATIONS.length; version += 1) {
    db.exec(MIGRATIONS[version]!);
    db.pragma(`user_version = ${version + 1}`);
  }
}

export class Store {
  constructor(private readonly db: Db) {}

  close(): void {
    this.db.close();
  }

  /* ---------------------------------------------------------------- 폴더 */

  listRoots(): Root[] {
    const rows = this.db
      .prepare(
        `SELECT id, path, included_count, excluded_count, added_at FROM roots ORDER BY added_at`,
      )
      .all() as {
      id: string;
      path: string;
      included_count: number;
      excluded_count: number;
      added_at: string;
    }[];
    return rows.map((r) => ({
      id: r.id,
      path: r.path,
      includedCount: r.included_count,
      excludedCount: r.excluded_count,
      addedAt: r.added_at,
    }));
  }

  upsertRoot(root: Root): void {
    this.db
      .prepare(
        `INSERT INTO roots (id, path, included_count, excluded_count, added_at)
         VALUES (@id, @path, @included, @excluded, @addedAt)
         ON CONFLICT(id) DO UPDATE SET
           path = excluded.path,
           included_count = excluded.included_count,
           excluded_count = excluded.excluded_count`,
      )
      .run({
        id: root.id,
        path: root.path,
        included: root.includedCount,
        excluded: root.excludedCount,
        addedAt: root.addedAt,
      });
  }

  /**
   * sidecar가 새 corpus를 준비한 뒤 폴더와 그 시점의 파일 snapshot을 한 번에 확정한다.
   * 중간 파일 insert가 실패하면 root까지 rollback되어 반쪽 등록 상태를 남기지 않는다.
   */
  replaceRootFiles(root: Root, rows: FileRow[]): void {
    const upsertRoot = this.db.prepare(
      `INSERT INTO roots (id, path, included_count, excluded_count, added_at)
       VALUES (@id, @path, @included, @excluded, @addedAt)
       ON CONFLICT(id) DO UPDATE SET
         path = excluded.path,
         included_count = excluded.included_count,
         excluded_count = excluded.excluded_count`,
    );
    const insertFile = this.db.prepare(
      `INSERT INTO files (root_id, rel_path, abs_path, sha256, size, mtime, status, dirty, deleted)
       VALUES (@rootId, @relPath, @absPath, @sha256, @size, @mtime, @status, @dirty, @deleted)`,
    );
    const tx = this.db.transaction((nextRoot: Root, files: FileRow[]) => {
      upsertRoot.run({
        id: nextRoot.id,
        path: nextRoot.path,
        included: nextRoot.includedCount,
        excluded: nextRoot.excludedCount,
        addedAt: nextRoot.addedAt,
      });
      this.db.prepare(`DELETE FROM files WHERE root_id = ?`).run(nextRoot.id);
      for (const row of files) {
        if (row.rootId !== nextRoot.id) {
          throw new Error('root snapshot에 다른 rootId의 파일을 저장할 수 없습니다.');
        }
        insertFile.run({
          rootId: row.rootId,
          relPath: row.relPath,
          absPath: row.absPath,
          sha256: row.sha256,
          size: row.size,
          mtime: row.mtime,
          status: row.status,
          dirty: row.dirty ? 1 : 0,
          deleted: row.deleted ? 1 : 0,
        });
      }
    });
    tx(root, rows);
  }

  removeRoot(id: string): void {
    const tx = this.db.transaction((rootId: string) => {
      this.db.prepare(`DELETE FROM files WHERE root_id = ?`).run(rootId);
      this.db.prepare(`DELETE FROM roots WHERE id = ?`).run(rootId);
    });
    tx(id);
  }

  /* ---------------------------------------------------------------- 파일 */

  listFiles(): FileRow[] {
    const rows = this.db
      .prepare(`SELECT * FROM files WHERE deleted = 0 ORDER BY root_id, rel_path`)
      .all() as RawFile[];
    return rows.map(toFileRow);
  }

  listDirtyFiles(): FileRow[] {
    const rows = this.db
      .prepare(`SELECT * FROM files WHERE dirty = 1 AND deleted = 0 ORDER BY root_id, rel_path`)
      .all() as RawFile[];
    return rows.map(toFileRow);
  }

  listDeletedFiles(): FileRow[] {
    const rows = this.db
      .prepare(`SELECT * FROM files WHERE deleted = 1 ORDER BY root_id, rel_path`)
      .all() as RawFile[];
    return rows.map(toFileRow);
  }

  findFile(rootId: string, relPath: string): FileRow | null {
    const row = this.db
      .prepare(`SELECT * FROM files WHERE root_id = ? AND rel_path = ?`)
      .get(rootId, relPath) as RawFile | undefined;
    return row ? toFileRow(row) : null;
  }

  upsertFiles(rows: FileRow[]): void {
    const stmt = this.db.prepare(
      `INSERT INTO files (root_id, rel_path, abs_path, sha256, size, mtime, status, dirty, deleted)
       VALUES (@rootId, @relPath, @absPath, @sha256, @size, @mtime, @status, @dirty, @deleted)
       ON CONFLICT(root_id, rel_path) DO UPDATE SET
         abs_path = excluded.abs_path,
         sha256   = excluded.sha256,
         size     = excluded.size,
         mtime    = excluded.mtime,
         status   = excluded.status,
         dirty    = excluded.dirty,
         deleted  = excluded.deleted`,
    );
    const tx = this.db.transaction((items: FileRow[]) => {
      for (const row of items) {
        stmt.run({
          rootId: row.rootId,
          relPath: row.relPath,
          absPath: row.absPath,
          sha256: row.sha256,
          size: row.size,
          mtime: row.mtime,
          status: row.status,
          dirty: row.dirty ? 1 : 0,
          deleted: row.deleted ? 1 : 0,
        });
      }
    });
    tx(rows);
  }

  setFileStatus(rootId: string, relPath: string, status: FileStatus): void {
    this.db
      .prepare(`UPDATE files SET status = ? WHERE root_id = ? AND rel_path = ?`)
      .run(status, rootId, relPath);
  }

  setStatusForDirty(status: FileStatus): void {
    this.db.prepare(`UPDATE files SET status = ? WHERE dirty = 1 AND deleted = 0`).run(status);
  }

  markFileDeleted(rootId: string, relPath: string): void {
    this.db
      .prepare(`UPDATE files SET deleted = 1, dirty = 1 WHERE root_id = ? AND rel_path = ?`)
      .run(rootId, relPath);
  }

  /** 빌드 성공 후 — dirty 해제, 상태 완료, 삭제 표시된 행 정리. */
  commitBuildResult(): void {
    const tx = this.db.transaction(() => {
      this.db.prepare(`DELETE FROM files WHERE deleted = 1`).run();
      this.db.prepare(`UPDATE files SET dirty = 0, status = 'done' WHERE dirty = 1`).run();
    });
    tx();
  }

  /** 빌드 실패 후 — 변환중이던 표시를 에러로 되돌린다 (silent fail 금지). */
  markDirtyAsError(): void {
    this.db.prepare(`UPDATE files SET status = 'error' WHERE dirty = 1 AND deleted = 0`).run();
  }

  /**
   * enrichment 보류 문서 (스펙 v1.4 §5).
   * `commitBuildResult()` 로 한 번 done 처리된 뒤 이 문서들만 다시 dirty 로 되돌려,
   * 다음 빌드에서 반드시 재시도되게 한다.
   */
  markFileDeferred(rootId: string, relPath: string): void {
    this.db
      .prepare(`UPDATE files SET dirty = 1, status = 'error' WHERE root_id = ? AND rel_path = ?`)
      .run(rootId, relPath);
  }

  /* ---------------------------------------------------------------- 빌드 */

  upsertBuild(row: BuildRow): void {
    this.db
      .prepare(
        `INSERT INTO builds (id, status, progress, message, error, deferred, created_at, updated_at)
         VALUES (@id, @status, @progress, @message, @error, @deferred, @createdAt, @updatedAt)
         ON CONFLICT(id) DO UPDATE SET
           status = excluded.status,
           progress = excluded.progress,
           message = excluded.message,
           error = excluded.error,
           deferred = excluded.deferred,
           updated_at = excluded.updated_at`,
      )
      .run(row);
  }

  /* ------------------------------------------------------------------ kv */

  getKv(key: string): string | null {
    const row = this.db.prepare(`SELECT value FROM kv WHERE key = ?`).get(key) as
      | { value: string }
      | undefined;
    return row?.value ?? null;
  }

  setKv(key: string, value: string): void {
    this.db
      .prepare(
        `INSERT INTO kv (key, value) VALUES (?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
      )
      .run(key, value);
  }

  deleteKv(key: string): void {
    this.db.prepare(`DELETE FROM kv WHERE key = ?`).run(key);
  }

  /* -------------------------------------------------------------- 대화 */

  listConversations(): Conversation[] {
    return this.db
      .prepare(`SELECT id, title, updated_at AS updatedAt FROM conversations ORDER BY updated_at DESC`)
      .all() as Conversation[];
  }

  createConversation(conversation: Conversation): void {
    this.db
      .prepare(
        `INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)`,
      )
      .run(conversation.id, conversation.title, conversation.updatedAt, conversation.updatedAt);
  }

  touchConversation(id: string, title?: string): void {
    const now = new Date().toISOString();
    if (title) {
      this.db
        .prepare(`UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?`)
        .run(now, title, id);
    } else {
      this.db.prepare(`UPDATE conversations SET updated_at = ? WHERE id = ?`).run(now, id);
    }
  }

  getConversation(id: string): Conversation | null {
    const row = this.db
      .prepare(`SELECT id, title, updated_at AS updatedAt FROM conversations WHERE id = ?`)
      .get(id) as Conversation | undefined;
    return row ?? null;
  }

  /* ------------------------------------------------------------ 메시지 */

  listMessages(conversationId: string): ChatMessage[] {
    const rows = this.db
      .prepare(
        `SELECT id, conversation_id, role, text, citations, tools, execution, changed_path,
                streaming, created_at
         FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid`,
      )
      .all(conversationId) as {
      id: string;
      conversation_id: string;
      role: string;
      text: string;
      citations: string | null;
      tools: string | null;
      execution: string | null;
      changed_path: string | null;
      streaming: number;
      created_at: string;
    }[];
    return rows.map((r) => ({
      id: r.id,
      conversationId: r.conversation_id,
      role: r.role as ChatRole,
      text: r.text,
      citations: parseJson<Citation[]>(r.citations) ?? undefined,
      tools: parseJson<{ name: string; summary: string }[]>(r.tools) ?? undefined,
      execution: parseJson<ExecutionSummary>(r.execution) ?? undefined,
      changedPath: r.changed_path ?? undefined,
      createdAt: r.created_at,
      streaming: r.streaming === 1 ? true : undefined,
    }));
  }

  insertMessage(message: ChatMessage): void {
    this.db
      .prepare(
        `INSERT INTO messages (
           id, conversation_id, role, text, citations, tools, execution, changed_path,
           streaming, created_at
         ) VALUES (
           @id, @conversationId, @role, @text, @citations, @tools, @execution, @changedPath,
           @streaming, @createdAt
         )`,
      )
      .run({
        id: message.id,
        conversationId: message.conversationId,
        role: message.role,
        text: message.text,
        citations: message.citations ? JSON.stringify(message.citations) : null,
        tools: message.tools ? JSON.stringify(message.tools) : null,
        execution: message.execution ? JSON.stringify(message.execution) : null,
        changedPath: message.changedPath ?? null,
        streaming: message.streaming ? 1 : 0,
        createdAt: message.createdAt,
      });
  }

  /** 스트리밍 도중 부분 저장 — 재시작해도 히스토리가 남는다. */
  updateMessage(
    id: string,
    patch: {
      text?: string;
      citations?: Citation[];
      tools?: { name: string; summary: string }[];
      execution?: ExecutionSummary;
      changedPath?: string;
      streaming?: boolean;
    },
  ): void {
    const sets: string[] = [];
    const params: Record<string, unknown> = { id };
    if (patch.text !== undefined) {
      sets.push('text = @text');
      params.text = patch.text;
    }
    if (patch.citations !== undefined) {
      sets.push('citations = @citations');
      params.citations = JSON.stringify(patch.citations);
    }
    if (patch.tools !== undefined) {
      sets.push('tools = @tools');
      params.tools = JSON.stringify(patch.tools);
    }
    if (patch.execution !== undefined) {
      sets.push('execution = @execution');
      params.execution = JSON.stringify(patch.execution);
    }
    if (patch.changedPath !== undefined) {
      sets.push('changed_path = @changedPath');
      params.changedPath = patch.changedPath;
    }
    if (patch.streaming !== undefined) {
      sets.push('streaming = @streaming');
      params.streaming = patch.streaming ? 1 : 0;
    }
    if (sets.length === 0) return;
    this.db.prepare(`UPDATE messages SET ${sets.join(', ')} WHERE id = @id`).run(params);
  }

  /** 앱이 강제 종료돼 streaming=1 로 남은 메시지 정리 (부팅 시 1회). */
  clearStaleStreaming(): void {
    this.db.prepare(`UPDATE messages SET streaming = 0 WHERE streaming = 1`).run();
  }
}

function toFileRow(raw: RawFile): FileRow {
  return {
    rootId: raw.root_id,
    relPath: raw.rel_path,
    absPath: raw.abs_path,
    sha256: raw.sha256,
    size: raw.size,
    mtime: raw.mtime,
    status: raw.status as FileStatus,
    dirty: raw.dirty === 1,
    deleted: raw.deleted === 1,
  };
}

function parseJson<T>(raw: string | null): T | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}
