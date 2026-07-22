/**
 * 등록 폴더의 원본을 doc2md로 정규화해 LLMWIKI 입력 저장소에 반영한다.
 *
 * LLMWIKI 입력 폴더는 앱이 소유한다. 문서 ID는 상대경로에서 만들기 때문에 원본 내용이
 * 바뀌어도 유지되고, source sha256이 같으면 이미 승인 과정에서 갱신된 입력을 덮어쓰지 않는다.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import type { FileRow } from '@main/db/store';
import { ensureDir, sha256, writeFileAtomic, writeJsonAtomic } from '@main/util/fsx';
import { crossLinkCorpus, type CrossLinkDoc } from './crosslink';

const GENERATED_ID_RE = /^DOC-[0-9A-F]{16}$/;
const LANGUAGE_RE = /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;
const OWNER_FILE = '.codegate-input-owner.json';
// 현재 고정된 LLMWIKI max_chars=2400보다 작게 유지해 overlap chunk를 만들지 않는다.
const LLMWIKI_SECTION_MAX_CHARS = 2_200;
const LLMWIKI_MAX_SECTIONS = 500;

interface Doc2MdResult {
  body?: unknown;
  source_sha256?: unknown;
  frontmatter?: {
    title?: unknown;
    language?: unknown;
    official_number?: unknown;
    authority_level?: unknown;
    issuing_org?: unknown;
    issued_on?: unknown;
    effective_from?: unknown;
    effective_to?: unknown;
  };
}

interface ExistingIdentity {
  id: string;
  title: string;
  revision: string;
  uri: string;
  sourceSha256: string;
}

interface OwnedDocument {
  id: string;
  relativePath: string;
  revision: string;
  sourceSha256: string;
}

interface OwnershipManifest {
  schemaVersion: 1;
  owner: 'codegate-local';
  documents: OwnedDocument[];
}

export interface InputSyncOptions {
  files: FileRow[];
  inputRoot: string;
  doc2mdUrl?: string;
  fetchImpl?: typeof fetch;
  onProgress?: (progress: InputSyncProgress) => void;
}

export interface InputSyncResult {
  converted: number;
  unchanged: number;
  removed: number;
}

export interface InputSyncProgress {
  done: number;
  total: number;
  relativePath?: string;
  result?: 'converted' | 'unchanged';
}

export function stableDocumentId(relativePath: string): string {
  return `DOC-${sha256(relativePath.normalize('NFC')).slice(0, 16).toUpperCase()}`;
}

export function canonicalSourceUri(relativePath: string): string {
  const segments = relativePath.replace(/\\/g, '/').split('/');
  if (segments.length === 0 || segments.some((segment) => !segment || segment === '.' || segment === '..')) {
    throw new Error(`안전하지 않은 원본 상대경로입니다: ${relativePath}`);
  }
  return `source://${segments.map((segment) => encodeURIComponent(segment)).join('/')}`;
}

export async function syncNormalizedInputs(options: InputSyncOptions): Promise<InputSyncResult> {
  const total = options.files.length;
  options.onProgress?.({ done: 0, total });
  await ensureDir(options.inputRoot);
  const ownership = await loadOwnership(options.inputRoot);
  // marker를 먼저 기록해 이후 어떤 파일이 앱 소유인지 crash 뒤에도 판별할 수 있게 한다.
  await writeJsonAtomic(path.join(options.inputRoot, OWNER_FILE), ownership);
  const fetchImpl = options.fetchImpl ?? fetch;
  const currentPaths = new Set(options.files.map((file) => file.relPath));
  const ownedStates = await Promise.all(
    ownership.documents.map(async (document) => {
      const identity = await readIdentity(path.join(options.inputRoot, `${document.id}.md`));
      return {
        ...document,
        revision: identity?.revision ?? document.revision,
        sourceSha256: identity?.sourceSha256 ?? document.sourceSha256,
      };
    }),
  );
  const nextDocuments: OwnedDocument[] = [];
  const usedIds = new Set<string>();
  const renameByPath = new Map<string, OwnedDocument>();
  const newFiles = options.files.filter(
    (file) => !ownedStates.some((document) => document.relativePath === file.relPath),
  );
  for (const file of newFiles) {
    const candidates = ownedStates.filter(
      (document) =>
        !currentPaths.has(document.relativePath) &&
        document.sourceSha256.toLowerCase() === file.sha256.toLowerCase(),
    );
    const sameHashNewFiles = newFiles.filter(
      (candidate) => candidate.sha256.toLowerCase() === file.sha256.toLowerCase(),
    );
    if (candidates.length === 1 && sameHashNewFiles.length === 1) {
      renameByPath.set(file.relPath, candidates[0]!);
    }
  }
  let converted = 0;
  let unchanged = 0;

  for (const [index, file] of options.files.entries()) {
    options.onProgress?.({ done: index, total, relativePath: file.relPath });
    const byPath = ownedStates.find((document) => document.relativePath === file.relPath);
    const previous = byPath ?? renameByPath.get(file.relPath);
    const id = previous?.id ?? stableDocumentId(file.relPath);
    if (!GENERATED_ID_RE.test(id) || usedIds.has(id)) {
      throw new Error(`normalized input 문서 ID가 중복되거나 올바르지 않습니다: ${id}`);
    }
    usedIds.add(id);
    const filename = `${id}.md`;
    const target = path.join(options.inputRoot, filename);
    const uri = canonicalSourceUri(file.relPath);

    const existing = await readIdentity(target);
    if (
      existing?.id === id &&
      existing.uri === uri &&
      existing.sourceSha256.toLowerCase() === file.sha256.toLowerCase() &&
      !(await normalizedInputNeedsRepair(target, existing.title))
    ) {
      unchanged += 1;
      nextDocuments.push({
        id,
        relativePath: file.relPath,
        revision: existing.revision,
        sourceSha256: file.sha256.toLowerCase(),
      });
      options.onProgress?.({
        done: index + 1,
        total,
        relativePath: file.relPath,
        result: 'unchanged',
      });
      continue;
    }
    if (!options.doc2mdUrl) {
      throw new Error(
        '새 문서를 변환할 doc2md가 연결되지 않았습니다. CODEGATE_DOC2MD_URL을 설정하거나 pnpm dev:integrated를 사용해 주세요.',
      );
    }

    const revision = nextRevision(existing?.revision ?? previous?.revision);
    const result = await convertSource({
      doc2mdUrl: options.doc2mdUrl,
      fetchImpl,
      file,
      id,
      revision,
      uri,
    });
    const title = normalizedTitle(result.frontmatter?.title, file.relPath);
    const body = normalizeWikiBody(String(result.body ?? ''), title);
    const frontmatter = {
      schema_version: '1.0.0',
      id,
      title,
      doc_type: 'general',
      language: normalizedLanguage(result.frontmatter?.language),
      revision,
      status: 'active',
      official_number: nullableString(result.frontmatter?.official_number),
      authority_level: normalizedAuthority(result.frontmatter?.authority_level),
      issuing_org: nullableString(result.frontmatter?.issuing_org),
      issued_on: nullableDate(result.frontmatter?.issued_on),
      effective_from: nullableDate(result.frontmatter?.effective_from),
      effective_to: nullableDate(result.frontmatter?.effective_to),
      source: {
        filename: file.relPath,
        uri,
        sha256: file.sha256.toLowerCase(),
      },
      access: 'internal',
      tags: [],
      aliases: [],
    };
    await writeFileAtomic(target, `---\n${JSON.stringify(frontmatter, null, 2)}\n---\n${body}`);
    nextDocuments.push({
      id,
      relativePath: file.relPath,
      revision,
      sourceSha256: file.sha256.toLowerCase(),
    });
    converted += 1;
    options.onProgress?.({
      done: index + 1,
      total,
      relativePath: file.relPath,
      result: 'converted',
    });
  }

  let removed = 0;
  for (const document of ownership.documents) {
    if (!usedIds.has(document.id) && GENERATED_ID_RE.test(document.id)) {
      await fs.rm(path.join(options.inputRoot, `${document.id}.md`), { force: true });
      removed += 1;
    }
  }
  await writeJsonAtomic(path.join(options.inputRoot, OWNER_FILE), {
    schemaVersion: 1,
    owner: 'codegate-local',
    documents: nextDocuments,
  } satisfies OwnershipManifest);
  // 문서를 전부 쓴 뒤에야 코퍼스 전체를 볼 수 있다 — 링크는 그때 넣는다.
  await applyCrossLinks(options.inputRoot, nextDocuments);
  return { converted, unchanged, removed };
}

/**
 * 문서끼리의 텍스트 참조를 마크다운 링크로 바꿔 준다.
 *
 * LLMWIKI 빌더는 `[제목](DOC-xxxx.md)` 링크에서만 관계(그래프 간선)를 뽑는다. 변환된
 * office 문서에는 그런 링크가 없고 `GBX-ONB-001` 같은 문서 번호를 글로 적을 뿐이라,
 * 상호 참조가 140군데 있어도 링크 후보가 0이 되어 간선이 하나도 생기지 않았다.
 *
 * 관계를 **만들어 내지 않는다.** 이미 본문에 적혀 있는 참조를 빌더가 알아볼 수 있는
 * 형태로 표기만 바꾼다. 어느 문서를 가리키는지 모호하면 손대지 않는다.
 */
async function applyCrossLinks(
  inputRoot: string,
  documents: OwnershipManifest['documents'],
): Promise<void> {
  const loaded: CrossLinkDoc[] = [];
  for (const document of documents) {
    const file = path.join(inputRoot, `${document.id}.md`);
    const raw = await fs.readFile(file, 'utf8').catch(() => null);
    if (raw === null) continue;
    // frontmatter 는 계약이다. 본문(두 번째 `---` 뒤)만 바꾼다.
    const end = raw.indexOf('\n---\n', raw.indexOf('---') + 3);
    if (end < 0) continue;
    loaded.push({
      id: document.id,
      label: document.relativePath,
      body: raw.slice(end + 5),
    });
  }

  const changed = crossLinkCorpus(loaded);
  for (const [id, body] of changed) {
    const file = path.join(inputRoot, `${id}.md`);
    const raw = await fs.readFile(file, 'utf8').catch(() => null);
    if (raw === null) continue;
    const end = raw.indexOf('\n---\n', raw.indexOf('---') + 3);
    if (end < 0) continue;
    await writeFileAtomic(file, raw.slice(0, end + 5) + body);
  }
}

async function loadOwnership(inputRoot: string): Promise<OwnershipManifest> {
  const marker = path.join(inputRoot, OWNER_FILE);
  const raw = await fs.readFile(marker, 'utf8').catch(() => null);
  if (raw === null) {
    const entries = await fs.readdir(inputRoot);
    if (entries.length > 0) {
      throw new Error('LLMWIKI input 경로는 Folding이 소유한 빈 디렉터리여야 합니다.');
    }
    return { schemaVersion: 1, owner: 'codegate-local', documents: [] };
  }
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error('LLMWIKI input ownership manifest가 손상됐습니다.');
  }
  if (!isOwnershipManifest(value)) {
    throw new Error('LLMWIKI input ownership manifest의 owner 또는 schema가 올바르지 않습니다.');
  }
  return value;
}

function isOwnershipManifest(value: unknown): value is OwnershipManifest {
  if (!value || typeof value !== 'object') return false;
  const manifest = value as Partial<OwnershipManifest>;
  return (
    manifest.schemaVersion === 1 &&
    manifest.owner === 'codegate-local' &&
    Array.isArray(manifest.documents) &&
    manifest.documents.every(
      (document) =>
        document &&
        typeof document.id === 'string' &&
        GENERATED_ID_RE.test(document.id) &&
        typeof document.relativePath === 'string' &&
        typeof document.revision === 'string' &&
        typeof document.sourceSha256 === 'string' &&
        /^[0-9a-f]{64}$/i.test(document.sourceSha256),
    )
  );
}

async function convertSource(input: {
  doc2mdUrl: string;
  fetchImpl: typeof fetch;
  file: FileRow;
  id: string;
  revision: string;
  uri: string;
}): Promise<Doc2MdResult> {
  const baseUrl = localDoc2MdBaseUrl(input.doc2mdUrl);
  const response = await input.fetchImpl(`${baseUrl}/v2/convert`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({
      source: { kind: 'path', path: input.file.absPath },
      metadata: {
        id: input.id,
        doc_type: 'general',
        revision: input.revision,
        status: 'active',
        uri: input.uri,
        access: 'internal',
        tags: [],
        aliases: [],
      },
      chunking: { enabled: false },
      expected_source_sha256: input.file.sha256,
    }),
    signal: AbortSignal.timeout(30 * 60 * 1_000),
  });
  const raw = await response.text();
  let payload: Doc2MdResult;
  try {
    payload = JSON.parse(raw) as Doc2MdResult;
  } catch {
    throw new Error(`doc2md가 JSON이 아닌 응답을 반환했습니다 (HTTP ${response.status}).`);
  }
  if (!response.ok) {
    throw new Error(`doc2md 변환에 실패했습니다 (HTTP ${response.status}): ${raw.slice(0, 500)}`);
  }
  if (
    typeof payload.body !== 'string' ||
    !payload.body.trim() ||
    payload.source_sha256 !== input.file.sha256
  ) {
    throw new Error('doc2md 응답의 본문 또는 원본 SHA-256 계약이 올바르지 않습니다.');
  }
  return payload;
}

export function localDoc2MdBaseUrl(value: string): string {
  const url = new URL(value);
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (
    url.protocol !== 'http:' ||
    !['127.0.0.1', 'localhost', '::1'].includes(host) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new Error('path 기반 doc2md 연결은 인증정보 없는 loopback HTTP 주소만 허용합니다.');
  }
  return url.toString().replace(/\/+$/, '');
}

function normalizeWikiBody(value: string, title: string): string {
  const body = deduplicateH2Headings(value.replace(/\r\n?/g, '\n').trim());
  if (!body) throw new Error('doc2md가 비어 있는 문서를 반환했습니다.');
  if (hasValidWikiSections(body, title)) return `${boundWikiSections(body)}\n`;

  let fenced: { marker: string; length: number } | null = null;
  const demoted = body.split('\n').map((line) => {
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fenced) {
      if (fence && fence[1]![0] === fenced.marker && fence[1]!.length >= fenced.length) {
        fenced = null;
      }
      return line;
    }
    if (fence) {
      fenced = { marker: fence[1]![0]!, length: fence[1]!.length };
      return line;
    }
    return line.replace(/^(#{1,6})([ \t]+)/, (_match, hashes: string, spacing: string) =>
      `${'#'.repeat(Math.min(6, hashes.length + 2))}${spacing}`,
    );
  });
  const wrapped = deduplicateH2Headings(`# ${title}\n\n## 본문\n\n${demoted.join('\n')}`);
  return `${boundWikiSections(wrapped)}\n`;
}

function deduplicateH2Headings(body: string): string {
  const used = new Set<string>();
  let fenced: { marker: string; length: number } | null = null;
  return body
    .split('\n')
    .map((line) => {
      const fence = line.match(/^\s*(`{3,}|~{3,})/);
      if (fenced) {
        if (fence && fence[1]![0] === fenced.marker && fence[1]!.length >= fenced.length) {
          fenced = null;
        }
        return line;
      }
      if (fence) {
        fenced = { marker: fence[1]![0]!, length: fence[1]!.length };
        return line;
      }
      const heading = line.match(/^##[ \t]+(.+?)[ \t]*$/);
      if (!heading?.[1]) return line;
      const base = heading[1].trim();
      let candidate = base;
      let occurrence = 2;
      while (used.has(candidate)) {
        candidate = `${base} · ${occurrence}`;
        occurrence += 1;
      }
      used.add(candidate);
      return `## ${candidate}`;
    })
    .join('\n');
}

async function normalizedInputNeedsRepair(target: string, title: string): Promise<boolean> {
  const raw = await fs.readFile(target, 'utf8').catch(() => null);
  if (!raw) return true;
  const body = raw.replace(/^---\s*\n[\s\S]*?\n---(?:\s*\n|$)/, '').trim();
  return (
    !hasValidWikiSections(body, title) ||
    deduplicateH2Headings(body) !== body ||
    boundWikiSections(body) !== body
  );
}

function boundWikiSections(body: string): string {
  const lines = body.split('\n');
  const headings: Array<{ index: number; title: string }> = [];
  let fenced: { marker: string; length: number } | null = null;
  for (const [index, line] of lines.entries()) {
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fenced) {
      if (fence && fence[1]![0] === fenced.marker && fence[1]!.length >= fenced.length) {
        fenced = null;
      }
      continue;
    }
    if (fence) {
      fenced = { marker: fence[1]![0]!, length: fence[1]!.length };
      continue;
    }
    const heading = line.match(/^##[ \t]+(.+?)[ \t]*$/);
    if (heading?.[1]) headings.push({ index, title: heading[1].trim() });
  }
  if (headings.length === 0) return body;

  const firstHeadingIndex = headings[0]!.index;
  const prefix = lines.slice(0, firstHeadingIndex);
  const h1Index = prefix.findIndex((line) => /^#[ \t]+/.test(line));
  const preamble = h1Index >= 0 ? prefix.slice(h1Index + 1) : [];
  const firstContent = lines.slice(firstHeadingIndex + 1, headings[1]?.index ?? lines.length);
  const movePreamble = [...preamble, ...firstContent].join('\n').trim().length > LLMWIKI_SECTION_MAX_CHARS;
  const output = movePreamble ? prefix.slice(0, h1Index + 1) : prefix;
  let sectionCount = 0;
  for (const [headingIndex, heading] of headings.entries()) {
    const end = headings[headingIndex + 1]?.index ?? lines.length;
    const originalHeading = lines[heading.index]!;
    const content = lines.slice(heading.index + 1, end);
    const parts = splitSectionLines(
      headingIndex === 0 && movePreamble ? [...preamble, ...content] : content,
    );
    for (const [partIndex, part] of parts.entries()) {
      sectionCount += 1;
      if (sectionCount > LLMWIKI_MAX_SECTIONS) {
        throw new Error(
          `문서 섹션이 LLMWIKI 제한(${LLMWIKI_MAX_SECTIONS}개)을 초과합니다. 원본을 여러 파일로 나눠 주세요.`,
        );
      }
      output.push(partIndex === 0 ? originalHeading : `## ${heading.title} · ${partIndex + 1}`);
      output.push(...part);
    }
  }
  return deduplicateH2Headings(output.join('\n').trim());
}

function splitSectionLines(lines: string[]): string[][] {
  if (lines.join('\n').trim().length <= LLMWIKI_SECTION_MAX_CHARS) return [lines];
  const parts: string[][] = [];
  let current: string[] = [];
  let currentLength = 0;
  const flush = () => {
    const trimmed = trimBlankLines(current);
    if (trimmed.length > 0) {
      if (trimmed.join('\n').length > LLMWIKI_SECTION_MAX_CHARS) {
        throw new Error(`정규화된 문서 섹션이 ${LLMWIKI_SECTION_MAX_CHARS}자를 초과합니다.`);
      }
      parts.push(trimmed);
    }
    current = [];
    currentLength = 0;
  };
  const append = (line: string) => {
    const addition = line.length + (current.length > 0 ? 1 : 0);
    if (
      current.some((candidate) => candidate.trim()) &&
      currentLength + addition > LLMWIKI_SECTION_MAX_CHARS
    ) {
      flush();
    }
    current.push(line);
    currentLength += line.length + (current.length > 1 ? 1 : 0);
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fence) {
      const marker = fence[1]!;
      const codeLines: string[] = [];
      let closingLine = marker;
      let closed = false;
      for (index += 1; index < lines.length; index += 1) {
        const candidate = lines[index]!;
        const closing = candidate.match(/^\s*(`{3,}|~{3,})\s*$/);
        if (closing && closing[1]![0] === marker[0] && closing[1]!.length >= marker.length) {
          closingLine = candidate;
          closed = true;
          break;
        }
        codeLines.push(candidate);
      }
      appendFencedBlock(line, codeLines, closingLine, append, flush, currentLength);
      if (!closed) index = lines.length;
      continue;
    }
    if (line.length > LLMWIKI_SECTION_MAX_CHARS) {
      for (const fragment of splitLongLine(line)) {
        append(fragment);
        flush();
      }
      continue;
    }
    append(line);
  }
  flush();
  return parts;
}

function appendFencedBlock(
  openingLine: string,
  codeLines: string[],
  closingLine: string,
  append: (line: string) => void,
  flush: () => void,
  currentLength: number,
): void {
  const overhead = openingLine.length + closingLine.length + 2;
  const capacity = LLMWIKI_SECTION_MAX_CHARS - overhead;
  if (capacity < 1) {
    throw new Error('코드 블록 fence가 너무 길어 문서 섹션으로 정규화할 수 없습니다.');
  }

  const wholeBlock = [openingLine, ...codeLines, closingLine];
  const available = LLMWIKI_SECTION_MAX_CHARS - (currentLength > 0 ? currentLength + 1 : 0);
  if (wholeBlock.join('\n').length <= available) {
    for (const line of wholeBlock) append(line);
    return;
  }
  if (currentLength > 0) flush();

  const fragments: string[] = [];
  for (const line of codeLines) {
    if (line.length <= capacity) {
      fragments.push(line);
      continue;
    }
    for (let offset = 0; offset < line.length; offset += capacity) {
      fragments.push(line.slice(offset, offset + capacity));
    }
  }
  if (fragments.length === 0) fragments.push('');

  let chunk: string[] = [];
  let chunkLength = 0;
  const flushChunk = () => {
    append(openingLine);
    for (const line of chunk) append(line);
    append(closingLine);
    flush();
    chunk = [];
    chunkLength = 0;
  };
  for (const fragment of fragments) {
    const addition = fragment.length + (chunk.length > 0 ? 1 : 0);
    if (chunk.length > 0 && chunkLength + addition > capacity) flushChunk();
    chunk.push(fragment);
    chunkLength += addition;
  }
  flushChunk();
}

function splitLongLine(line: string): string[] {
  const fragments: string[] = [];
  let remaining = line;
  while (remaining.length > LLMWIKI_SECTION_MAX_CHARS) {
    const candidate = remaining.slice(0, LLMWIKI_SECTION_MAX_CHARS);
    const whitespace = candidate.lastIndexOf(' ');
    const end = whitespace >= LLMWIKI_SECTION_MAX_CHARS / 2 ? whitespace : candidate.length;
    fragments.push(remaining.slice(0, end).trimEnd());
    remaining = remaining.slice(end).trimStart();
  }
  if (remaining) fragments.push(remaining);
  return fragments;
}

function trimBlankLines(lines: string[]): string[] {
  let start = 0;
  let end = lines.length;
  while (start < end && !lines[start]!.trim()) start += 1;
  while (end > start && !lines[end - 1]!.trim()) end -= 1;
  return lines.slice(start, end);
}

function hasValidWikiSections(body: string, title: string): boolean {
  const lines = body.split('\n');
  const headings: { index: number; level: number; title: string }[] = [];
  let fenced: { marker: string; length: number } | null = null;
  for (const [index, line] of lines.entries()) {
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fenced) {
      if (fence && fence[1]![0] === fenced.marker && fence[1]!.length >= fenced.length) {
        fenced = null;
      }
      continue;
    }
    if (fence) {
      fenced = { marker: fence[1]![0]!, length: fence[1]!.length };
      continue;
    }
    const heading = line.match(/^(#{1,6})[ \t]+(.+?)[ \t]*$/);
    if (heading) headings.push({ index, level: heading[1]!.length, title: heading[2]!.trim() });
  }
  const h1 = headings.filter((heading) => heading.level === 1);
  const h2 = headings.filter((heading) => heading.level === 2);
  if (h1.length !== 1 || h1[0]?.title !== title || h2.length === 0) return false;
  return h2.every((heading, index) => {
    const end = h2[index + 1]?.index ?? lines.length;
    return lines.slice(heading.index + 1, end).some((line) => line.trim() && !/^<a id=/.test(line));
  });
}

async function readIdentity(target: string): Promise<ExistingIdentity | null> {
  const raw = await fs.readFile(target, 'utf8').catch(() => null);
  if (!raw) return null;
  const match = raw.match(/^---\s*\n([\s\S]*?)\n---(?:\s*\n|$)/);
  if (!match?.[1]) return null;
  const front = match[1].trim();
  try {
    const parsed = JSON.parse(front) as {
      id?: unknown;
      title?: unknown;
      revision?: unknown;
      source?: { uri?: unknown; sha256?: unknown };
    };
    if (
      typeof parsed.id === 'string' &&
      typeof parsed.title === 'string' &&
      typeof parsed.revision === 'string' &&
      typeof parsed.source?.uri === 'string' &&
      typeof parsed.source.sha256 === 'string'
    ) {
      return {
        id: parsed.id,
        title: parsed.title,
        revision: parsed.revision,
        uri: parsed.source.uri,
        sourceSha256: parsed.source.sha256,
      };
    }
  } catch {
    // sidecar가 다시 쓴 YAML front matter는 아래 제한된 parser로 읽는다.
  }
  const id = yamlScalar(front, 'id');
  const title = yamlScalar(front, 'title');
  const revision = yamlScalar(front, 'revision');
  const source = front.match(/^source:\s*\n((?:[ \t]+.*(?:\n|$))*)/m)?.[1] ?? '';
  const uri = yamlScalar(source, 'uri');
  const sourceSha256 = yamlScalar(source, 'sha256');
  return id && title && revision && uri && sourceSha256
    ? { id, title, revision, uri, sourceSha256 }
    : null;
}

function yamlScalar(value: string, key: string): string | null {
  const match = value.match(new RegExp(`^\\s*${key}:\\s*(.+?)\\s*$`, 'm'))?.[1]?.trim();
  if (!match || match === 'null' || match === '~') return null;
  if ((match.startsWith('"') && match.endsWith('"')) || (match.startsWith("'") && match.endsWith("'"))) {
    return match.slice(1, -1);
  }
  return match;
}

function nextRevision(current?: string): string {
  if (!current) return '1';
  const numeric = Number(current);
  return Number.isSafeInteger(numeric) && numeric >= 0 ? String(numeric + 1) : `${current}.1`;
}

function normalizedTitle(value: unknown, relativePath: string): string {
  const fallback = path.basename(relativePath, path.extname(relativePath));
  const title = (typeof value === 'string' ? value : fallback).replace(/[\r\n]+/g, ' ').trim();
  return (title || fallback || '문서').slice(0, 500);
}

function normalizedLanguage(value: unknown): string {
  return typeof value === 'string' && LANGUAGE_RE.test(value) ? value : 'und';
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function nullableDate(value: unknown): string | null {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

function normalizedAuthority(value: unknown): string | null {
  const allowed = new Set([
    'regulation',
    'policy',
    'contract',
    'procedure',
    'manual',
    'guide',
    'specification',
    'report',
    'reference',
  ]);
  // LLMWIKI source schema는 null을 허용하지만 Backend compatibility manifest는 문자열을 요구한다.
  return typeof value === 'string' && allowed.has(value) ? value : 'reference';
}
