/**
 * 내장 목 에이전트 — `@codegate/agent`(성주) 가 아직 없을 때 앱을 데모 가능한 상태로 유지한다.
 * 계약의 `Agent` 를 그대로 구현하고, 실제와 같은 순서로 `AgentEvent` 를 흘린다:
 *
 *   tool_start(wiki_search) → tool_start(read_original) → text_delta × N
 *   → (쓰기 요청이면) approval_request + approvalHandler 왕복 → done(citations)
 *
 * 승인 없이는 kordoc 을 절대 호출하지 않는다 (스펙 v1.3 §5 승인 우회 금지).
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {
  NOT_FOUND_IN_WIKI,
  formatCitation,
  type Agent,
  type AgentDeps,
  type AgentEvent,
  type Citation,
} from '@contracts';
import { logError, toUserMessage } from '@main/util/errors';

/** 위키 빌드 산출물의 문서 목록 (목 백엔드가 만들어내는 `manifest.json`). */
interface WikiManifest {
  docs: {
    docId: string;
    rev: number;
    title: string;
    /** 위키 산출물 안의 마크다운 상대 경로 */
    file: string;
    /** 원본 파일의 가상 경로 — 인용 클릭 시 이 경로로 원본을 연다 */
    sourcePath?: string;
    sections?: string[];
  }[];
}

/**
 * 쓰기 의도가 있는 질문인지 판별 — 목에서만 쓰는 단순 휴리스틱.
 * 스펙 v1.3 §3 데모 문장("…자문계약서 **만들어줘**")이 반드시 걸려야 한다.
 */
const WRITE_INTENT = /(수정|고쳐|채워|작성|추가|삽입|바꿔|교체|생성|만들|복제|새로 만)/;

export function createMockAgent(deps: AgentDeps): Agent {
  return {
    send(userText: string): AsyncIterable<AgentEvent> {
      return run(deps, userText);
    },
  };
}

async function* run(deps: AgentDeps, userText: string): AsyncGenerator<AgentEvent> {
  const query = userText.trim();

  yield { type: 'tool_start', name: 'wiki_search', summary: `위키에서 「${shorten(query)}」 검색` };
  await sleep(320);

  const docs = await loadWikiDocs(deps.config.wikiDir);
  if (docs.length === 0) {
    yield { type: 'text_delta', text: `${NOT_FOUND_IN_WIKI}.\n\n` };
    yield {
      type: 'text_delta',
      text: '아직 위키 빌드가 없습니다. 폴더를 등록하고 빌드를 한 번 돌린 뒤 다시 물어봐 주세요.',
    };
    yield { type: 'done', citations: [] };
    return;
  }

  const picked = docs.slice(0, 2);
  yield {
    type: 'tool_start',
    name: 'read_original',
    summary: `원본 확인 — ${picked.map((d) => d.title).join(', ')}`,
  };
  await sleep(260);

  const citations: Citation[] = picked.map((doc) => ({
    docId: doc.docId,
    rev: doc.rev,
    section: doc.sections?.[0] ?? '본문',
    sourcePath: doc.sourcePath,
  }));

  for (const chunk of composeAnswer(query, picked, citations)) {
    yield { type: 'text_delta', text: chunk };
    await sleep(45);
  }

  if (WRITE_INTENT.test(query)) {
    const target = picked[0]?.sourcePath ?? picked[0]?.file ?? '문서';
    const diff = makeUnifiedDiff(target);

    // 렌더러가 승인 모달을 띄우도록 이벤트를 먼저 흘린다
    yield { type: 'approval_request', diff, target };

    // 그리고 반드시 승인 왕복을 기다린다 — 이 await 없이 쓰기는 일어나지 않는다
    const approved = await deps.approvalHandler({
      tool: 'patch_document',
      target,
      diff,
      rationale: '위키에서 확인한 조항과 문구를 맞추기 위한 수정입니다.',
    });

    if (!approved) {
      yield { type: 'text_delta', text: '\n\n수정을 취소했습니다. 원본은 그대로 두었습니다.' };
      yield { type: 'done', citations };
      return;
    }

    const applied = await applyPatch(deps, target);
    yield { type: 'text_delta', text: `\n\n${applied}` };
  }

  yield { type: 'done', citations };
}

/** 승인된 뒤에만 호출된다. 대상이 실제 파일일 때만 kordoc 을 태운다. */
async function applyPatch(deps: AgentDeps, target: string): Promise<string> {
  const absPath = resolveInRoots(deps.config.roots, target);
  if (!absPath) {
    return '승인은 확인했지만, 등록된 폴더 안에서 대상 문서를 찾지 못해 수정하지 않았습니다.';
  }
  try {
    const md = await deps.kordoc.parse(absPath);
    const result = await deps.kordoc.patch(absPath, `${md}\n\n<!-- codegate 수정 반영 -->\n`);
    if (!result.ok) {
      // exit 2 = 부분 실패. 성공으로 삼키지 않는다 (스펙 v1.3 §5).
      return `일부 편집이 적용되지 않았습니다 (미적용 ${result.unapplied.length}건). 백업: ${path.basename(result.backupPath)}`;
    }
    return `수정을 적용했습니다. 백업은 ${path.basename(result.backupPath)} 에 남겨두었습니다.`;
  } catch (err) {
    logError('mock-agent:patch', err);
    return toUserMessage(err, '문서를 수정하지 못했습니다. 원본은 그대로 두었습니다.');
  }
}

function resolveInRoots(roots: string[], virtualOrRelPath: string): string | null {
  const tail = virtualOrRelPath.split('/').slice(1).join('/') || virtualOrRelPath;
  for (const root of roots) {
    const candidate = path.join(root, tail);
    if (candidate.startsWith(path.resolve(root))) return candidate;
  }
  return null;
}

async function loadWikiDocs(wikiDir: string): Promise<WikiManifest['docs']> {
  for (const candidate of [
    path.join(wikiDir, 'current', 'manifest.json'),
    path.join(wikiDir, 'manifest.json'),
  ]) {
    try {
      const raw = await fs.readFile(candidate, 'utf8');
      const parsed = JSON.parse(raw) as WikiManifest;
      if (Array.isArray(parsed.docs) && parsed.docs.length > 0) return parsed.docs;
    } catch {
      // 다음 후보로
    }
  }
  return [];
}

function composeAnswer(
  query: string,
  docs: WikiManifest['docs'],
  citations: Citation[],
): string[] {
  const titles = docs.map((d) => `「${d.title}」`).join(' 와 ');
  return [
    `${titles} 에서 관련 내용을 찾았습니다.\n\n`,
    `질문하신 "${shorten(query)}" 에 대해 정리하면 다음과 같습니다.\n\n`,
    '1. 위키에 등록된 문서 기준으로 해당 항목이 확인됩니다.\n',
    '2. 관련 조항은 원본 문서의 해당 섹션에 그대로 남아 있습니다.\n',
    '3. 위키에서 확인되지 않은 내용은 추정하지 않았습니다.\n\n',
    `근거: ${citations.map(formatCitation).join(' ')}`,
  ];
}

function makeUnifiedDiff(target: string): string {
  return [
    `--- a/${target}`,
    `+++ b/${target}`,
    '@@ -12,7 +12,7 @@',
    ' 제3조 (대금의 지급)',
    ' ',
    '-① 발주자는 계약 체결일로부터 30일 이내에 대금을 지급한다.',
    '+① 발주자는 계약 체결일로부터 15일 이내에 대금을 지급한다.',
    ' ',
    ' ② 지급이 지연된 경우 지연이자를 가산한다.',
    '',
  ].join('\n');
}

function shorten(text: string, max = 24): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
