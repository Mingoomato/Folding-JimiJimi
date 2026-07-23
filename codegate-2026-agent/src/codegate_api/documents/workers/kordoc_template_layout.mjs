const LEFT_COLUMN_UNITS = 30;
const RIGHT_COLUMN_UNITS = 30;

function stripInlineMarkdown(value) {
  return value
    .replace(/^\s*[-*+]\s+/, '• ')
    .replace(/^\s*\d+[.)]\s+/, '• ')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .trim();
}

function reportColumns(markdown) {
  const current = [];
  const next = [];
  let target = current;
  for (const raw of String(markdown ?? '').split(/\r?\n/)) {
    const normalized = raw.trim();
    const heading =
      /^#{1,6}\s+(.+)$/.exec(normalized) ??
      /^(?:\*\*|__)([^*_\n]+?)(?:\*\*|__)\s*:?$/.exec(normalized);
    if (heading) {
      const label = stripInlineMarkdown(heading[1]);
      if (/^(다음|명일)\s*업무/.test(label)) {
        target = next;
        continue;
      }
      if (target === current && !/^(업무\s*보고서|보고서)$/.test(label)) {
        target.push(label);
      }
      continue;
    }
    const line = stripInlineMarkdown(raw);
    if (line) target.push(line);
  }
  return { current, next };
}

function displayUnits(value) {
  return Array.from(value).reduce(
    (total, character) => total + (character.codePointAt(0) <= 0x7f ? 1 : 2),
    0,
  );
}

function splitWord(word, maxUnits) {
  const chunks = [];
  let chunk = '';
  for (const character of Array.from(word)) {
    if (chunk && displayUnits(chunk + character) > maxUnits) {
      chunks.push(chunk);
      chunk = character;
    } else {
      chunk += character;
    }
  }
  if (chunk) chunks.push(chunk);
  return chunks;
}

export function wrapCellLine(value, maxUnits) {
  const words = String(value).replace(/\s+/g, ' ').trim().split(' ').filter(Boolean);
  const lines = [];
  let line = '';
  for (const word of words) {
    const pieces = displayUnits(word) > maxUnits ? splitWord(word, maxUnits) : [word];
    for (const piece of pieces) {
      const candidate = line ? `${line} ${piece}` : piece;
      if (line && displayUnits(candidate) > maxUnits) {
        lines.push(line);
        line = piece;
      } else {
        line = candidate;
      }
    }
  }
  if (line) lines.push(line);
  return lines;
}

function layoutColumn(lines, maxUnits, rowCount, label) {
  const wrapped = lines.map((line) => wrapCellLine(line, maxUnits));
  const laidOut = wrapped.flat();
  if (laidOut.length <= rowCount) {
    return { lines: laidOut, compactedLines: 0 };
  }
  if (lines.length > rowCount) {
    throw new Error(
      `template_content_overflow: ${label} has ${lines.length} items; template has ${rowCount} rows`,
    );
  }
  const allocations = wrapped.map(() => 1);
  let remainingRows = rowCount - lines.length;
  while (remainingRows > 0) {
    let allocated = false;
    for (let index = 0; index < wrapped.length && remainingRows > 0; index += 1) {
      if (allocations[index] < wrapped[index].length) {
        allocations[index] += 1;
        remainingRows -= 1;
        allocated = true;
      }
    }
    if (!allocated) break;
  }
  const compactedLines = wrapped.filter(
    (segments, index) => allocations[index] < segments.length,
  ).length;
  return {
    lines: wrapped.flatMap((segments, index) => {
      const kept = segments.slice(0, allocations[index]);
      if (allocations[index] < segments.length) {
        kept[kept.length - 1] = truncateToUnits(`${kept[kept.length - 1]}…`, maxUnits);
      }
      return kept;
    }),
    compactedLines,
  };
}

function truncateToUnits(value, maxUnits) {
  if (displayUnits(value) <= maxUnits) return value;
  const suffix = '…';
  const targetUnits = maxUnits - displayUnits(suffix);
  let result = '';
  for (const character of Array.from(value)) {
    if (displayUnits(result + character) > targetUnits) break;
    result += character;
  }
  return result.trimEnd() + suffix;
}

function escapeHtml(value) {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

export function fillDailyWorkTemplate(templateMarkdown, reportMarkdown) {
  if (!reportMarkdown) return { markdown: templateMarkdown, inserted: [] };
  const columns = reportColumns(reportMarkdown);
  const header = /<tr>[^\n]*금일\s*업무내용[^\n]*명일\s*업무내용[^\n]*<\/tr>/;
  const headerMatch = header.exec(templateMarkdown);
  if (!headerMatch) return { markdown: templateMarkdown, inserted: [] };

  const blankRow = '<tr><td colspan="2"></td><td colspan="5"></td></tr>';
  const start = headerMatch.index + headerMatch[0].length;
  let cursor = start;
  let rowCount = 0;
  while (templateMarkdown.startsWith(`\n${blankRow}`, cursor)) {
    rowCount += 1;
    cursor += blankRow.length + 1;
  }
  if (rowCount === 0) return { markdown: templateMarkdown, inserted: [] };

  const leftLayout = layoutColumn(columns.current, LEFT_COLUMN_UNITS, rowCount, '금일 업무내용');
  const rightLayout = layoutColumn(columns.next, RIGHT_COLUMN_UNITS, rowCount, '명일 업무내용');
  const left = leftLayout.lines;
  const right = rightLayout.lines;
  const rows = Array.from({ length: rowCount }, (_, index) => (
    `<tr><td colspan="2">${escapeHtml(left[index] ?? '')}</td>`
      + `<td colspan="5">${escapeHtml(right[index] ?? '')}</td></tr>`
  ));
  const filled = `${templateMarkdown.slice(0, start)}\n${rows.join('\n')}${templateMarkdown.slice(cursor)}`;
  return {
    markdown: filled,
    verifiedLines: left.concat(right),
    inserted: [
      ...(left.length ? ['금일 업무내용'] : []),
      ...(right.length ? ['명일 업무내용'] : []),
    ],
    layout: {
      rowCount,
      leftRowsUsed: left.length,
      rightRowsUsed: right.length,
      compactedLines: leftLayout.compactedLines + rightLayout.compactedLines,
      overflowPolicy: 'compact_then_fail_closed',
    },
  };
}
