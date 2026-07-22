import { describe, expect, it } from 'vitest';
import { parseDotEnv, loadDotEnv } from '@main/env';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * 설정 파일은 `.env` 하나뿐이다. 지키는 약속:
 *   · 실제 환경변수가 이긴다 — `.env` 는 빈 자리만 채운다
 *   · `export `·따옴표·주석을 흘려도 값이 깨지지 않는다
 *   · 파일이 없으면 조용히 넘어간다 (선택 사항)
 */
describe('parseDotEnv', () => {
  it('KEY=value, export 접두어, 주석, 빈 줄을 처리한다', () => {
    const parsed = parseDotEnv(
      ['# 주석', '', 'CODEGATE_AUTH_MODE=cloud', 'export FOO=bar', '  BAZ = qux  '].join('\n'),
    );
    expect(parsed).toEqual({ CODEGATE_AUTH_MODE: 'cloud', FOO: 'bar', BAZ: 'qux' });
  });

  it('따옴표는 벗기고, 감싸지 않은 값의 인라인 주석은 잘라 낸다', () => {
    const parsed = parseDotEnv(['A="sk-ant-1 2 3"', "B='literal #hash'", 'C=raw # 꼬리 주석'].join('\n'));
    expect(parsed.A).toBe('sk-ant-1 2 3');
    expect(parsed.B).toBe('literal #hash');
    expect(parsed.C).toBe('raw');
  });

  it('키 모양이 아니면 무시한다 — 잘못된 줄이 전체를 깨지 않는다', () => {
    expect(parseDotEnv(['not a config line', '=오른쪽만', '1BAD=x', 'OK=y'].join('\n'))).toEqual({
      OK: 'y',
    });
  });
});

describe('loadDotEnv', () => {
  it('빈 자리만 채우고 이미 있는 환경변수는 건드리지 않는다', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codegate-env-'));
    fs.writeFileSync(path.join(dir, '.env'), 'ALREADY=fromfile\nNEW=fromfile\n');
    const env: NodeJS.ProcessEnv = { ALREADY: 'fromshell' };

    const result = loadDotEnv(dir, env);

    expect(env.ALREADY).toBe('fromshell'); // 실제 환경변수 우선
    expect(env.NEW).toBe('fromfile'); // 빈 자리만 채움
    expect(result.applied).toEqual(['NEW']);
  });

  it('파일이 없으면 조용히 넘어간다', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codegate-env-'));
    const result = loadDotEnv(dir, {});
    expect(result.file).toBeNull();
    expect(result.applied).toEqual([]);
  });
});
