import { useCallback, useEffect, useState } from 'react';
import type { BuildState, FileNode, Root } from '@contracts';

/** L2 — 등록 폴더 · 빌드 상태 · 파일 트리. 모두 메인이 진실의 원천이다. */
export function useWorkspace() {
  const [roots, setRoots] = useState<Root[]>([]);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [build, setBuild] = useState<BuildState | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      window.codegate.roots.list(),
      window.codegate.tree.get(),
      window.codegate.build.state(),
    ])
      .then(([r, t, b]) => {
        if (!alive) return;
        setRoots(r);
        setTree(t);
        setBuild(b);
      })
      .finally(() => alive && setLoading(false));

    const offTree = window.codegate.tree.onChanged(setTree);
    const offBuild = window.codegate.build.onChanged(setBuild);
    return () => {
      alive = false;
      offTree();
      offBuild();
    };
  }, []);

  const addRoot = useCallback(async (path: string) => {
    const root = await window.codegate.roots.add(path);
    setRoots(await window.codegate.roots.list());
    return root;
  }, []);

  const removeRoot = useCallback(async (id: string) => {
    await window.codegate.roots.remove(id);
    setRoots(await window.codegate.roots.list());
  }, []);

  return {
    roots,
    tree,
    build,
    loading,
    addRoot,
    removeRoot,
    pickFolder: window.codegate.roots.pickDialog,
    scanPreview: window.codegate.roots.scanPreview,
    triggerBuild: window.codegate.build.trigger,
  };
}
