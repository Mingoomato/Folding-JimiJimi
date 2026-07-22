import type { ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { describe, expect, it, vi } from 'vitest';
import {
  SidecarSupervisor,
  buildSidecarArgs,
  buildSidecarEnvironment,
  normalizeSidecarOrigin,
  validateSidecarRuntimePaths,
  type SidecarRuntimePaths,
} from '@main/sidecar/supervisor';

const runtime: SidecarRuntimePaths = {
  sourceRoot: '/workspace/source',
  llmwikiProjectRoot: '/app/LLMWIKI',
  llmwikiInputRoot: '/workspace/source-md',
  llmwikiStorageRoot: '/app-data/llmwiki',
  dataRoot: '/app-data/codegate',
  doc2mdUrl: 'http://127.0.0.1:9000',
  deterministicAgent: true,
  claudeModel: 'sonnet',
  tenantId: 'local',
  wikiId: 'workspace',
};

describe('SidecarSupervisor contract', () => {
  it('Backend CLI가 요구하는 runtime 인자를 모두 만든다', () => {
    expect(buildSidecarArgs('http://127.0.0.1:43123', 'app://codegate', runtime)).toEqual([
      '--source-root',
      '/workspace/source',
      '--llmwiki-project-root',
      '/app/LLMWIKI',
      '--llmwiki-input-root',
      '/workspace/source-md',
      '--llmwiki-storage-root',
      '/app-data/llmwiki',
      '--data-root',
      '/app-data/codegate',
      '--port',
      '43123',
      '--cors-origin',
      'app://codegate',
      '--tenant-id',
      'local',
      '--wiki-id',
      'workspace',
      '--doc2md-url',
      'http://127.0.0.1:9000',
      '--deterministic-agent',
    ]);
  });

  it('CLI import 전 local runtime invariant를 환경으로 고정한다', () => {
    const environment = buildSidecarEnvironment(
      {
        PATH: '/usr/bin',
        HOME: '/Users/test',
        CODEGATE_ENVIRONMENT: 'production',
        CODEGATE_SUPABASE_SERVICE_ROLE_KEY: 'must-not-cross-process-boundary',
      },
      'app://codegate',
      runtime,
      'test-anthropic-key',
      'test-gemini-key',
      { CODEGATE_BOOTSTRAP_DEMO: 'true' },
    );

    expect(environment).toMatchObject({
      PATH: '/usr/bin',
      CODEGATE_ENVIRONMENT: 'local',
      CODEGATE_AUTH_MODE: 'local',
      CODEGATE_BOOTSTRAP_DEMO: 'false',
      CODEGATE_KNOWLEDGE_MODE: 'llmwiki',
      CODEGATE_SOURCE_ROOT: '/workspace/source',
      CODEGATE_RUNTIME_ROOT: '/app-data/codegate',
      CODEGATE_LLMWIKI_PROJECT_ROOT: '/app/LLMWIKI',
      CODEGATE_LLMWIKI_INPUT_ROOT: '/workspace/source-md',
      CODEGATE_LLMWIKI_STORAGE_ROOT: '/app-data/llmwiki',
      CODEGATE_LLMWIKI_TENANT_ID: 'local',
      CODEGATE_LLMWIKI_WIKI_ID: 'workspace',
      CODEGATE_CORS_ORIGINS: '["app://codegate"]',
      CODEGATE_AGENT_MODE: 'deterministic',
      CODEGATE_CLAUDE_MODEL: 'sonnet',
      CODEGATE_CLAUDE_TIMEOUT_SECONDS: '180',
      CODEGATE_DOC2MD_BASE_URL: 'http://127.0.0.1:9000',
      HOME: '/app-data/codegate',
      USERPROFILE: '/app-data/codegate',
      ANTHROPIC_API_KEY: 'test-anthropic-key',
    });
    expect(environment.CODEGATE_SUPABASE_SERVICE_ROLE_KEY).toBeUndefined();
  });

  it('Gemini 모드는 Anthropic 키 없이 필요한 Gemini 설정만 sidecar에 넘긴다', () => {
    const geminiRuntime: SidecarRuntimePaths = {
      ...runtime,
      deterministicAgent: undefined,
      agentMode: 'gemini',
      geminiModel: 'gemini-2.5-flash-lite',
      geminiTimeoutSeconds: 90,
      geminiDataPolicy: 'paid-no-training',
    };
    const environment = buildSidecarEnvironment(
      {
        PATH: '/usr/bin',
        CODEGATE_GOOGLE_CLIENT_SECRET: 'must-not-cross-process-boundary',
        CODEGATE_SESSION_PEPPER: 'must-not-cross-process-boundary',
      },
      'app://codegate',
      geminiRuntime,
      '',
      'test-gemini-key',
    );

    expect(buildSidecarArgs('http://127.0.0.1:43123', 'app://codegate', geminiRuntime)).not.toContain(
      '--deterministic-agent',
    );
    expect(environment).toMatchObject({
      CODEGATE_AGENT_MODE: 'gemini',
      CODEGATE_GEMINI_MODEL: 'gemini-2.5-flash-lite',
      CODEGATE_GEMINI_TIMEOUT_SECONDS: '90',
      CODEGATE_GEMINI_DATA_POLICY: 'paid-no-training',
      GEMINI_API_KEY: 'test-gemini-key',
      GOOGLE_API_KEY: 'test-gemini-key',
    });
    expect(environment.ANTHROPIC_API_KEY).toBeUndefined();
    expect(environment.CODEGATE_GOOGLE_CLIENT_SECRET).toBeUndefined();
    expect(environment.CODEGATE_SESSION_PEPPER).toBeUndefined();
  });

  it('외부 API base를 origin으로 정규화하고 health URL을 한 번만 붙인다', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ status: 'ok', agent_available: true }), { status: 200 }),
    );
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      externalUrl: 'http://127.0.0.1:8000/api/v1/',
      onState: vi.fn(),
      fetchImpl: fetchImpl as typeof fetch,
    });

    await expect(supervisor.prepare()).resolves.toBe('http://127.0.0.1:8000');
    await supervisor.start();

    expect(fetchImpl).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/health',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(supervisor.getState().status).toBe('external');
  });

  it('외부 엔진 health가 회복되면 unhealthy 상태를 external로 되돌린다', async () => {
    vi.useFakeTimers();
    let healthy = false;
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      externalUrl: 'http://127.0.0.1:8000',
      onState: vi.fn(),
      fetchImpl: vi.fn(async () =>
        healthy
          ? new Response(JSON.stringify({ status: 'ok', agent_available: true }), { status: 200 })
          : new Response('not ready', { status: 503 }),
      ) as typeof fetch,
    });

    try {
      await supervisor.prepare();
      const failedStart = supervisor.start();
      await vi.advanceTimersByTimeAsync(30_500);
      await failedStart;
      expect(supervisor.getState().status).toBe('unhealthy');

      healthy = true;
      await supervisor.restart();
      expect(supervisor.getState().status).toBe('external');
      expect(supervisor.getState().error).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it('문서 폴더가 정해지기 전에는 managed sidecar를 실행하지 않는다', async () => {
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      onState: vi.fn(),
    });
    await supervisor.prepare();
    await supervisor.start();

    expect(supervisor.configured).toBe(false);
    expect(supervisor.getState().status).toBe('stopped');
    expect(supervisor.getState().message).toContain('문서 폴더');
  });

  it('API path가 없는 외부 주소도 그대로 정규화한다', () => {
    expect(normalizeSidecarOrigin('http://127.0.0.1:8000/')).toBe('http://127.0.0.1:8000');
  });

  it('health 응답 직후 process가 끝나면 ready로 잘못 전이하지 않는다', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-supervisor-'));
    const sourceRoot = path.join(root, 'source');
    const llmwikiProjectRoot = path.join(root, 'LLMWIKI');
    await Promise.all([
      fs.mkdir(sourceRoot, { recursive: true }),
      fs.mkdir(llmwikiProjectRoot, { recursive: true }),
    ]);
    const child = Object.assign(new EventEmitter(), {
      stdout: new PassThrough(),
      stderr: new PassThrough(),
      stdin: null,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      killed: false,
      kill: vi.fn(() => true),
    }) as unknown as ChildProcess;
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      onState: vi.fn(),
      spawnImpl: vi.fn(() => child) as unknown as typeof import('node:child_process').spawn,
      fetchImpl: vi.fn(async () => {
        (child as unknown as { exitCode: number | null }).exitCode = 1;
        return new Response(JSON.stringify({ status: 'ok', agent_available: true }), {
          status: 200,
        });
      }) as typeof fetch,
    });
    supervisor.configure({
      sourceRoot,
      llmwikiProjectRoot,
      llmwikiInputRoot: path.join(root, 'source-md'),
      llmwikiStorageRoot: path.join(root, 'storage'),
      dataRoot: path.join(root, 'data'),
      deterministicAgent: true,
    });

    try {
      await supervisor.start();
      expect(supervisor.getState().status).toBe('crashed');
      expect(supervisor.getState().error).toContain('health 확인 직후');
    } finally {
      consoleError.mockRestore();
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('source와 input 경로가 겹치면 어떤 파일도 쓰기 전에 거부한다', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-path-safety-'));
    const sourceRoot = path.join(root, 'source');
    const llmwikiProjectRoot = path.join(root, 'LLMWIKI');
    const original = path.join(sourceRoot, 'DOC-FFFFFFFFFFFFFFFF.md');
    await Promise.all([
      fs.mkdir(sourceRoot, { recursive: true }),
      fs.mkdir(llmwikiProjectRoot, { recursive: true }),
    ]);
    await fs.writeFile(original, 'original');

    try {
      await expect(
        validateSidecarRuntimePaths({
          sourceRoot,
          llmwikiProjectRoot,
          llmwikiInputRoot: sourceRoot,
          llmwikiStorageRoot: path.join(root, 'storage'),
          dataRoot: path.join(root, 'data'),
        }),
      ).rejects.toThrow('겹칠 수 없습니다');
      await expect(fs.readFile(original, 'utf8')).resolves.toBe('original');
      await expect(fs.stat(path.join(sourceRoot, '.codegate-input-owner.json'))).rejects.toThrow();
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('dispose는 crash 뒤 예약된 자동 재시작을 취소한다', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-dispose-'));
    const sourceRoot = path.join(root, 'source');
    const llmwikiProjectRoot = path.join(root, 'LLMWIKI');
    await Promise.all([
      fs.mkdir(sourceRoot, { recursive: true }),
      fs.mkdir(llmwikiProjectRoot, { recursive: true }),
    ]);
    const child = Object.assign(new EventEmitter(), {
      stdout: new PassThrough(),
      stderr: new PassThrough(),
      stdin: null,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      killed: false,
      kill: vi.fn(() => true),
    }) as unknown as ChildProcess;
    const spawnImpl = vi.fn(() => child) as unknown as typeof import('node:child_process').spawn;
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      onState: vi.fn(),
      spawnImpl,
      fetchImpl: vi.fn(async () =>
        new Response(JSON.stringify({ status: 'ok', agent_available: true }), { status: 200 }),
      ) as typeof fetch,
    });
    supervisor.configure({
      sourceRoot,
      llmwikiProjectRoot,
      llmwikiInputRoot: path.join(root, 'source-md'),
      llmwikiStorageRoot: path.join(root, 'storage'),
      dataRoot: path.join(root, 'data'),
      deterministicAgent: true,
    });

    try {
      await supervisor.start();
      (child as unknown as { exitCode: number | null }).exitCode = 1;
      child.emit('exit', 1, null);
      expect(supervisor.getState().status).toBe('restarting');
      await supervisor.dispose();
      await new Promise((resolve) => setTimeout(resolve, 600));
      expect(spawnImpl).toHaveBeenCalledTimes(1);
      expect(supervisor.getState().status).toBe('stopped');
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('health 확인 중 dispose하면 30초 timeout을 기다리지 않고 즉시 취소한다', async () => {
    let requestAborted = false;
    const fetchImpl = vi.fn(
      async (_input: string | URL | Request, init?: RequestInit): Promise<Response> =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => {
              requestAborted = true;
              reject(new Error('aborted'));
            },
            { once: true },
          );
        }),
    );
    const supervisor = new SidecarSupervisor({
      corsOrigin: 'app://codegate',
      externalUrl: 'http://127.0.0.1:8000',
      onState: vi.fn(),
      fetchImpl: fetchImpl as typeof fetch,
    });
    await supervisor.prepare();
    const start = supervisor.start();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());

    const startedAt = Date.now();
    await supervisor.dispose();
    await start;

    expect(requestAborted).toBe(true);
    expect(Date.now() - startedAt).toBeLessThan(1_000);
    expect(supervisor.getState().status).toBe('stopped');
  });

  it.runIf(process.platform !== 'win32')(
    'leader 비정상 종료 뒤 같은 process group의 descendant까지 정리한다',
    async () => {
      const root = await fs.mkdtemp(path.join(os.tmpdir(), 'codegate-process-tree-'));
      const sourceRoot = path.join(root, 'source');
      const llmwikiProjectRoot = path.join(root, 'LLMWIKI');
      const runner = path.join(root, 'runner.sh');
      const descendantPids = path.join(root, 'descendants.txt');
      await Promise.all([
        fs.mkdir(sourceRoot, { recursive: true }),
        fs.mkdir(llmwikiProjectRoot, { recursive: true }),
      ]);
      const quotedNode = process.execPath.replaceAll("'", "'\\''");
      const quotedPids = descendantPids.replaceAll("'", "'\\''");
      await fs.writeFile(
        runner,
        `#!/bin/sh\n'${quotedNode}' -e 'setInterval(() => {}, 1000)' &\necho "$!" >> '${quotedPids}'\nsleep 0.2\nexit 7\n`,
        { mode: 0o755 },
      );
      const supervisor = new SidecarSupervisor({
        corsOrigin: 'app://codegate',
        binary: runner,
        onState: vi.fn(),
        fetchImpl: vi.fn(async () =>
          new Response(JSON.stringify({ status: 'ok', agent_available: true }), { status: 200 }),
        ) as typeof fetch,
      });
      supervisor.configure({
        sourceRoot,
        llmwikiProjectRoot,
        llmwikiInputRoot: path.join(root, 'source-md'),
        llmwikiStorageRoot: path.join(root, 'storage'),
        dataRoot: path.join(root, 'data'),
        deterministicAgent: true,
      });

      try {
        await supervisor.start();
        await vi.waitFor(() => expect(supervisor.getState().status).toBe('restarting'), {
          timeout: 2_000,
        });
        await supervisor.dispose();
        const pids = (await fs.readFile(descendantPids, 'utf8'))
          .trim()
          .split('\n')
          .map(Number);
        expect(pids.length).toBeGreaterThan(0);
        for (const pid of pids) expect(isProcessRunning(pid)).toBe(false);
      } finally {
        await supervisor.dispose();
        await fs.rm(root, { recursive: true, force: true });
      }
    },
  );
});

function isProcessRunning(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== 'ESRCH';
  }
}
