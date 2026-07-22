/**
 * `.env` 로더 — 설정 파일은 **하나뿐이다.**
 *
 * 예전에는 두 갈래였다. `pnpm dev:integrated` 는 `.env.integration` 을 읽고, 그냥
 * `pnpm dev` 로 띄우면 셸에 export 한 값만 보였다. 그래서 "분명 설정했는데 앱은 모른다"가
 * 반복됐다. 지금은 실행 방식과 상관없이 프로젝트 루트의 `.env` 하나를 읽는다.
 *
 * 우선순위는 **실제 환경변수가 이긴다.** `.env` 는 비어 있는 자리를 채울 뿐이다.
 * 그래야 `CODEGATE_DOC2MD_URL=... pnpm dev` 같은 일회성 덮어쓰기가 그대로 먹고,
 * dev:integrated 가 동적으로 잡은 포트를 `.env` 의 낡은 값이 뒤엎지 않는다.
 *
 * 파일이 없으면 조용히 넘어간다 — `.env` 는 선택이지 필수가 아니다.
 */
import fs from 'node:fs';
import path from 'node:path';

export interface DotEnvResult {
  /** 실제로 읽은 파일 경로. 파일이 없으면 null */
  file: string | null;
  /** 이번에 채워 넣은 키 이름들 (값은 담지 않는다 — 로그로 새면 안 된다) */
  applied: string[];
}

/** `KEY=value` 한 줄을 해석한다. 주석·빈 줄·`export ` 접두어·따옴표를 처리한다. */
export function parseDotEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const withoutExport = line.startsWith('export ') ? line.slice(7).trim() : line;
    const eq = withoutExport.indexOf('=');
    if (eq <= 0) continue;
    const key = withoutExport.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let value = withoutExport.slice(eq + 1).trim();
    // 따옴표로 감싼 값은 벗겨 낸다. 감싸지 않은 값의 `#` 뒤는 주석으로 본다.
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length > 1) ||
      (value.startsWith("'") && value.endsWith("'") && value.length > 1)
    ) {
      value = value.slice(1, -1);
    } else {
      const hash = value.indexOf(' #');
      if (hash >= 0) value = value.slice(0, hash).trim();
    }
    out[key] = value;
  }
  return out;
}

/**
 * `.env` 를 읽어 **비어 있는 환경변수만** 채운다.
 * 이미 값이 있는 키는 건드리지 않는다 (실제 환경변수 우선).
 */
export function loadDotEnv(projectDir: string, env: NodeJS.ProcessEnv = process.env): DotEnvResult {
  const file = path.join(projectDir, '.env');
  let text: string;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return { file: null, applied: [] };
  }
  const applied: string[] = [];
  for (const [key, value] of Object.entries(parseDotEnv(text))) {
    if (env[key] !== undefined) continue;
    env[key] = value;
    applied.push(key);
  }
  return { file, applied };
}
