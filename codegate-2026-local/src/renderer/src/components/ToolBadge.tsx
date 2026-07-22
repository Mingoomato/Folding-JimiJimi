import { FilePlus2, FileText, Image, PenLine, Search, Wrench } from 'lucide-react';
import type { ReactNode } from 'react';

/**
 * 도구 실행 배지 (tool_start).
 * 디자인 시스템 규칙에 따라 이모지 대신 Lucide 아이콘만 쓴다.
 */
const TOOLS: Record<string, { icon: ReactNode; label: string }> = {
  wiki_search: { icon: <Search size={13} />, label: '위키 검색' },
  read_original: { icon: <FileText size={13} />, label: '원문 확인' },
  render_page: { icon: <Image size={13} />, label: '페이지 렌더' },
  patch_document: { icon: <PenLine size={13} />, label: '문서 수정' },
  create_document: { icon: <FilePlus2 size={13} />, label: '문서 생성' },
  create_from_template: { icon: <FilePlus2 size={13} />, label: '양식 복제' },
};

export function ToolBadge({ name, summary }: { name: string; summary: string }) {
  const t = TOOLS[name] ?? { icon: <Wrench size={13} />, label: name };
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-ink-50 py-1 pl-2 pr-2.5 text-2xs text-ink-500">
      <span className="shrink-0 text-ink-400">{t.icon}</span>
      <span className="shrink-0 font-semibold text-ink-600">{t.label}</span>
      {summary && <span className="truncate text-ink-400">{summary}</span>}
    </span>
  );
}
