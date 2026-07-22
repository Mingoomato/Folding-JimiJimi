/**
 * chokidar 워처 (스펙 v1.3 §1 L2).
 * 저장 폭풍(한 번에 여러 파일 저장, 오피스의 임시파일→rename 패턴)이 매번 재빌드를 부르지 않도록
 * 이벤트를 debounce 로 합쳐서 한 번만 흘려보낸다.
 */
import path from 'node:path';
import { watch, type FSWatcher } from 'chokidar';
import type { Root } from '@contracts';
import { logError } from '@main/util/errors';
import { toPosix } from '@main/util/vpath';
import { classifyFile, makeChokidarIgnore } from './rules';

export type WatchChangeKind = 'add' | 'change' | 'unlink';

export interface WatchChange {
  rootId: string;
  rootPath: string;
  relPath: string;
  absPath: string;
  kind: WatchChangeKind;
}

export interface FileWatcherOptions {
  /** 변경 폭주를 합칠 시간(ms). 기본 1.5초. */
  debounceMs?: number;
  onChanges: (changes: WatchChange[]) => void;
}

export class FileWatcher {
  private readonly watchers = new Map<string, FSWatcher>();
  private readonly pending = new Map<string, WatchChange>();
  private timer: NodeJS.Timeout | null = null;
  private readonly debounceMs: number;

  constructor(private readonly options: FileWatcherOptions) {
    this.debounceMs = options.debounceMs ?? 1500;
  }

  /** 등록 폴더 목록에 맞춰 감시를 추가/해제한다. */
  async sync(roots: Root[]): Promise<void> {
    const wanted = new Set(roots.map((r) => r.id));
    for (const [rootId, watcher] of this.watchers) {
      if (!wanted.has(rootId)) {
        this.watchers.delete(rootId);
        await watcher.close();
      }
    }
    for (const root of roots) {
      if (this.watchers.has(root.id)) continue;
      this.watchers.set(root.id, this.createWatcher(root));
    }
  }

  async close(): Promise<void> {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const watchers = [...this.watchers.values()];
    this.watchers.clear();
    await Promise.all(watchers.map((w) => w.close()));
  }

  private createWatcher(root: Root): FSWatcher {
    const watcher = watch(root.path, {
      ignored: makeChokidarIgnore(root.path),
      ignoreInitial: true,
      // 오피스 앱은 저장을 여러 번에 나눠 하므로 안정될 때까지 기다린다
      awaitWriteFinish: { stabilityThreshold: 700, pollInterval: 120 },
      followSymlinks: false,
    });

    const push = (kind: WatchChangeKind) => (absPath: string) => {
      const rel = toPosix(path.relative(root.path, absPath));
      if (!rel || !classifyFile(rel).included) return;
      this.enqueue({ rootId: root.id, rootPath: root.path, relPath: rel, absPath, kind });
    };

    watcher.on('add', push('add'));
    watcher.on('change', push('change'));
    watcher.on('unlink', push('unlink'));
    watcher.on('error', (err) => logError('watcher', err));
    return watcher;
  }

  private enqueue(change: WatchChange): void {
    // 같은 파일의 연속 이벤트는 마지막 것만 남긴다 (add→change→unlink 순서 보존)
    this.pending.set(`${change.rootId}/${change.relPath}`, change);
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), this.debounceMs);
  }

  private flush(): void {
    this.timer = null;
    if (this.pending.size === 0) return;
    const changes = [...this.pending.values()];
    this.pending.clear();
    try {
      this.options.onChanges(changes);
    } catch (err) {
      logError('watcher:flush', err);
    }
  }
}

export { scanPreview, indexRoot, describeFile } from './scan';
export { classifyFile, SUPPORTED_EXTENSIONS } from './rules';
