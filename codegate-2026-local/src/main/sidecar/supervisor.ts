/**
 * sidecar 프로세스 수명주기 (스펙 v2.1 §1 L2).
 *
 * `codegate-local`(Python)은 그냥 프로그램이라 누군가 켜주고 지켜봐야 한다. 그게 여기다.
 *   · 빈 포트를 잡고 `--port`·`--cors-origin` 과 함께 띄운다
 *   · `GET /health` 가 200 을 줄 때까지 기다린다 (그전에 요청을 보내면 실패한다)
 *   · 비정상 종료를 감지해 백오프를 두고 되살린다
 *   · 앱이 끝나면 같이 내린다 — **파이썬을 고아 프로세스로 남기지 않는다**
 *
 * 비즈니스 로직은 하나도 두지 않는다. 띄우고·기다리고·되살리고·내리는 것이 전부다.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs/promises';
import { createServer } from 'node:net';
import path from 'node:path';
import type { SidecarLifecycleState, SidecarStatus } from '@contracts';
import { SIDECAR_STATUS_MESSAGE } from '@contracts';
import { logError } from '@main/util/errors';

/** `/health` 가 200 을 줄 때까지 이만큼만 기다린다. */
const DEFAULT_READY_TIMEOUT_MS = 30_000;
const HEALTH_INTERVAL_MS = 300;
/** 이 횟수를 넘기면 자동 재기동을 포기하고 사용자에게 알린다. */
const MAX_RESTARTS = 3;
/** 종료 신호 뒤 이만큼 기다렸다가 강제 종료. */
const KILL_GRACE_MS = 3_000;
/** 기동 실패 원인을 판단할 만큼만 stderr 를 남긴다. */
const STDERR_TAIL_LIMIT = 8_192;

/**
 * 위키가 지금 등록 폴더와 어긋났을 때 sidecar 가 내는 신호.
 *
 * 활성 빌드가 참조하는 원본이 사라지면(폴더를 바꾸거나 파일을 지우면) sidecar 는 기동
 * 자체를 거부한다. 이 경우 앱은 **못 뜬 채로 남고 사용자가 손쓸 방법이 없다.**
 */
const STALE_WIKI_SIGNATURE = /source document is missing|KnowledgePackageError/;

export interface SidecarSupervisorOptions {
  /** 렌더러 오리진 — sidecar CORS 를 여기로 좁힌다 */
  corsOrigin: string;
  onState: (state: SidecarLifecycleState) => void;
  /** 실행 파일. 없으면 PATH 의 `codegate-local` */
  binary?: string;
  /**
   * 이미 떠 있는 sidecar 에 붙을 때(개발 중 수동 실행 등).
   * 지정되면 **spawn 하지 않는다** — 남의 프로세스를 관리하려 들지 않는다.
   */
  externalUrl?: string;
  /** 자식에게 넘길 추가 환경변수 (경로 등) */
  extraEnv?: Record<string, string>;
  /** 앱이 안전하게 복호화한 Anthropic 키. 없으면 process 환경변수를 사용한다. */
  resolveAnthropicApiKey?: () => Promise<string | null>;
  /**
   * 위키 enrichment(문서 사이 관계 = 그래프 간선)용 Gemini 키.
   * 빌더가 Gemini 로 만들어져 있어 Anthropic 키만으로는 간선이 생기지 않는다.
   */
  resolveGeminiApiKey?: () => Promise<string | null>;
  /** 테스트에서 health 확인을 갈아끼우기 위한 구멍 */
  fetchImpl?: typeof fetch;
  /** 테스트에서 실제 process 대신 제어 가능한 child를 주입한다. */
  spawnImpl?: typeof spawn;
}

export interface SidecarRuntimePaths {
  sourceRoot: string;
  llmwikiProjectRoot: string;
  llmwikiInputRoot: string;
  llmwikiStorageRoot: string;
  dataRoot: string;
  doc2mdUrl?: string;
  /** sidecar의 답변 에이전트. deterministicAgent는 이전 설정과의 호환용이다. */
  agentMode?: 'deterministic' | 'claude' | 'gemini';
  deterministicAgent?: boolean;
  claudeModel?: string;
  claudeTimeoutSeconds?: number;
  geminiModel?: string;
  geminiTimeoutSeconds?: number;
  geminiDataPolicy?: 'paid-no-training' | 'development-free';
  tenantId?: string;
  wikiId?: string;
}

export function normalizeSidecarOrigin(value: string): string {
  const url = new URL(value);
  const normalizedPath = url.pathname.replace(/\/+$/, '').replace(/\/api\/v1$/, '');
  url.pathname = normalizedPath || '/';
  url.search = '';
  url.hash = '';
  return url.toString().replace(/\/$/, '');
}

export function buildSidecarArgs(
  baseUrl: string,
  corsOrigin: string,
  runtime: SidecarRuntimePaths,
): string[] {
  const url = new URL(baseUrl);
  const args = [
    '--source-root',
    runtime.sourceRoot,
    '--llmwiki-project-root',
    runtime.llmwikiProjectRoot,
    '--llmwiki-input-root',
    runtime.llmwikiInputRoot,
    '--llmwiki-storage-root',
    runtime.llmwikiStorageRoot,
    '--data-root',
    runtime.dataRoot,
    '--port',
    url.port,
    '--cors-origin',
    corsOrigin,
  ];
  if (runtime.tenantId) args.push('--tenant-id', runtime.tenantId);
  if (runtime.wikiId) args.push('--wiki-id', runtime.wikiId);
  if (runtime.doc2mdUrl) args.push('--doc2md-url', runtime.doc2mdUrl);
  if (effectiveAgentMode(runtime) === 'deterministic') args.push('--deterministic-agent');
  return args;
}

export function buildSidecarEnvironment(
  baseEnv: NodeJS.ProcessEnv,
  corsOrigin: string,
  runtime: SidecarRuntimePaths,
  anthropicApiKey: string,
  geminiApiKey: string,
  extraEnv: Record<string, string> = {},
): NodeJS.ProcessEnv {
  return {
    ...pickSafeProcessEnvironment(baseEnv),
    ...extraEnv,
    // codegate-local의 CLI module은 인자 파싱 전에 기본 ASGI app을 import한다.
    // 따라서 CLI 인자와 같은 local invariant를 환경에도 먼저 제공해야 한다.
    CODEGATE_ENVIRONMENT: 'local',
    CODEGATE_AUTH_MODE: 'local',
    CODEGATE_BOOTSTRAP_DEMO: 'false',
    CODEGATE_KNOWLEDGE_MODE: 'llmwiki',
    CODEGATE_SOURCE_ROOT: runtime.sourceRoot,
    CODEGATE_RUNTIME_ROOT: runtime.dataRoot,
    CODEGATE_LLMWIKI_PROJECT_ROOT: runtime.llmwikiProjectRoot,
    CODEGATE_LLMWIKI_INPUT_ROOT: runtime.llmwikiInputRoot,
    CODEGATE_LLMWIKI_STORAGE_ROOT: runtime.llmwikiStorageRoot,
    CODEGATE_LLMWIKI_TENANT_ID: runtime.tenantId ?? 'local',
    CODEGATE_LLMWIKI_WIKI_ID: runtime.wikiId ?? 'workspace',
    ...(baseEnv.CODEGATE_LLMWIKI_STARTUP_ENRICHMENT
      ? {
          CODEGATE_LLMWIKI_STARTUP_ENRICHMENT:
            baseEnv.CODEGATE_LLMWIKI_STARTUP_ENRICHMENT,
        }
      : {}),
    CODEGATE_CORS_ORIGINS: JSON.stringify([corsOrigin]),
    CODEGATE_AGENT_MODE: effectiveAgentMode(runtime),
    ...(runtime.claudeModel ? { CODEGATE_CLAUDE_MODEL: runtime.claudeModel } : {}),
    CODEGATE_CLAUDE_TIMEOUT_SECONDS: String(runtime.claudeTimeoutSeconds ?? 180),
    ...(runtime.geminiModel ? { CODEGATE_GEMINI_MODEL: runtime.geminiModel } : {}),
    ...(runtime.geminiTimeoutSeconds
      ? { CODEGATE_GEMINI_TIMEOUT_SECONDS: String(runtime.geminiTimeoutSeconds) }
      : {}),
    ...(runtime.geminiDataPolicy
      ? { CODEGATE_GEMINI_DATA_POLICY: runtime.geminiDataPolicy }
      : {}),
    ...(runtime.doc2mdUrl ? { CODEGATE_DOC2MD_BASE_URL: runtime.doc2mdUrl } : {}),
    HOME: runtime.dataRoot,
    USERPROFILE: runtime.dataRoot,
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    NO_PROXY: '127.0.0.1,localhost,::1',
    // Supabase token은 넘기지 않는다. 선택한 provider 키만 sidecar process에 한정한다.
    // Gemini 모드에서 빈 Anthropic 변수를 만들지 않는다 — 해당 키를 요구하는 설정으로
    // 오인하면 Gemini 키만 넣은 통합 실행이 시작 단계에서 막힌다.
    ...(anthropicApiKey ? { ANTHROPIC_API_KEY: anthropicApiKey } : {}),
    // 위키 enrichment(간선)는 Gemini 를 쓴다. 없으면 관계가 만들어지지 않아 그래프가
    // 점만 남는다 — 실제로 links.jsonl 이 0바이트로 나왔던 원인이다.
    ...(geminiApiKey ? { GEMINI_API_KEY: geminiApiKey, GOOGLE_API_KEY: geminiApiKey } : {}),
    // 한글(HWP/HWPX) 원본 수정에 쓰는 kordoc(Node 라이브러리)이 설치된 디렉터리.
    // 없으면 sidecar 가 cwd 에서 찾다 실패하고 "kordoc 을 찾지 못했습니다" 로 멈춘다.
    ...(baseEnv.CODEGATE_KORDOC_DIR ? { CODEGATE_KORDOC_DIR: baseEnv.CODEGATE_KORDOC_DIR } : {}),
    ...(baseEnv.CODEGATE_NODE_BIN ? { CODEGATE_NODE_BIN: baseEnv.CODEGATE_NODE_BIN } : {}),
    ...(baseEnv.CODEGATE_KORDOC_WORKSPACE_ROOT
      ? { CODEGATE_KORDOC_WORKSPACE_ROOT: baseEnv.CODEGATE_KORDOC_WORKSPACE_ROOT }
      : {}),
  };
}

function effectiveAgentMode(runtime: SidecarRuntimePaths): 'deterministic' | 'claude' | 'gemini' {
  return runtime.agentMode ?? (runtime.deterministicAgent ? 'deterministic' : 'claude');
}

function pickSafeProcessEnvironment(baseEnv: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const safe: NodeJS.ProcessEnv = {};
  for (const name of [
    'PATH',
    'Path',
    'PATHEXT',
    'SystemRoot',
    'SYSTEMROOT',
    'WINDIR',
    'TMPDIR',
    'TMP',
    'TEMP',
    'LANG',
    'LC_ALL',
    'TZ',
    'SSL_CERT_FILE',
    'SSL_CERT_DIR',
  ]) {
    if (baseEnv[name] !== undefined) safe[name] = baseEnv[name];
  }
  return safe;
}

/** 비어 있는 loopback 포트를 하나 잡아 준다. */
export function allocatePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const address = srv.address();
      if (typeof address === 'string' || !address) {
        srv.close(() => reject(new Error('포트를 할당하지 못했습니다.')));
        return;
      }
      const { port } = address;
      srv.close(() => resolve(port));
    });
  });
}

export class SidecarSupervisor {
  private child: ChildProcess | null = null;
  private readonly managedTrees = new Set<ChildProcess>();
  private baseUrl: string | null = null;
  private stopping = false;
  private readonly fetchImpl: typeof fetch;
  private readonly spawnImpl: typeof spawn;
  private spawnError: Error | null = null;
  private runtime: SidecarRuntimePaths | null = null;
  private state: SidecarLifecycleState;
  private lifecycle: Promise<void> = Promise.resolve();
  private restartTimer: NodeJS.Timeout | null = null;
  private generation = 0;
  private disposed = false;
  private readinessAbort: AbortController | null = null;
  private stderrTail = '';

  constructor(private readonly options: SidecarSupervisorOptions) {
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.spawnImpl = options.spawnImpl ?? spawn;
    this.state = {
      status: 'stopped',
      baseUrl: null,
      message: SIDECAR_STATUS_MESSAGE.stopped,
      restarts: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  /** 마지막 기동에서 sidecar 가 남긴 stderr 꼬리. */
  get recentStderr(): string {
    return this.stderrTail;
  }

  /** 위키가 지금 폴더와 어긋나 기동하지 못한 것인가 — 그렇다면 다시 만들면 살아난다. */
  get failedOnStaleWiki(): boolean {
    return STALE_WIKI_SIGNATURE.test(this.stderrTail);
  }

  clearRecentStderr(): void {
    this.stderrTail = '';
  }

  getState(): SidecarLifecycleState {
    return this.state;
  }

  /** 우리가 띄운 프로세스인지 (외부 연결이면 false). */
  get managed(): boolean {
    return !this.options.externalUrl;
  }

  get configured(): boolean {
    return this.runtime !== null;
  }

  configure(runtime: SidecarRuntimePaths): void {
    this.runtime = runtime;
  }

  clearRuntime(): void {
    this.runtime = null;
  }

  private setState(status: SidecarStatus, patch: Partial<SidecarLifecycleState> = {}): void {
    this.state = {
      ...this.state,
      status,
      message: patch.message ?? SIDECAR_STATUS_MESSAGE[status],
      baseUrl: patch.baseUrl !== undefined ? patch.baseUrl : this.baseUrl,
      updatedAt: new Date().toISOString(),
      ...patch,
    };
    this.options.onState(this.state);
  }

  /**
   * base URL 을 확정한다. **spawn 하기 전에** 부른다 —
   * 포트를 먼저 잡아 두면 재기동해도 URL 이 바뀌지 않아, 이미 만들어진 클라이언트가 그대로 산다.
   */
  async prepare(): Promise<string> {
    if (this.options.externalUrl) {
      this.baseUrl = normalizeSidecarOrigin(this.options.externalUrl);
      this.setState('external', { baseUrl: this.baseUrl });
      return this.baseUrl;
    }
    const port = await allocatePort();
    this.baseUrl = `http://127.0.0.1:${port}`;
    return this.baseUrl;
  }

  /** 띄우고 준비될 때까지 기다린다. 외부 연결 모드면 health 확인만 한다. */
  async start(): Promise<void> {
    return this.enqueue(() => this.startUnlocked());
  }

  private async startUnlocked(): Promise<void> {
    if (this.disposed) return;
    if (
      this.child &&
      this.child.exitCode === null &&
      this.child.signalCode === null &&
      ['starting', 'ready'].includes(this.state.status)
    ) return;
    if (!this.baseUrl) await this.prepare();
    this.stopping = false;

    if (!this.managed) {
      const readiness = new AbortController();
      this.readinessAbort = readiness;
      try {
        await this.waitForHealth(readiness.signal);
        this.setState('external', { error: undefined });
      } catch (err) {
        if (readiness.signal.aborted || this.disposed || this.stopping) return;
        this.setState('unhealthy', {
          error: err instanceof Error ? err.message : '외부 엔진에 연결하지 못했습니다.',
        });
      } finally {
        if (this.readinessAbort === readiness) this.readinessAbort = null;
      }
      return;
    }

    if (!this.runtime) {
      this.setState('stopped', {
        message: '문서 폴더를 선택하면 엔진을 시작합니다.',
        error: undefined,
      });
      return;
    }

    this.setState('starting');
    const readiness = new AbortController();
    this.readinessAbort = readiness;
    try {
      await prepareRuntimePaths(this.runtime);
      const storedKey = await this.options.resolveAnthropicApiKey?.();
      const geminiKey = await this.options.resolveGeminiApiKey?.();
      this.spawnError = null;
      this.spawnChild(
        storedKey ?? process.env.ANTHROPIC_API_KEY ?? '',
        geminiKey ?? process.env.GEMINI_API_KEY ?? '',
      );
      await this.waitForHealth(readiness.signal);
      if (
        this.managed &&
        (!this.child || this.child.exitCode !== null || this.child.signalCode !== null)
      ) {
        throw new Error('엔진 process가 health 확인 직후 종료됐습니다.');
      }
      this.setState('ready');
    } catch (err) {
      const cancelled = readiness.signal.aborted || this.disposed || this.stopping;
      if (!cancelled) logError('sidecar:start', err);
      await this.stopChild();
      if (!cancelled) {
        this.setState('crashed', {
          error: err instanceof Error ? err.message : '엔진을 시작하지 못했습니다.',
        });
      }
    } finally {
      if (this.readinessAbort === readiness) this.readinessAbort = null;
    }
  }

  /** 사용자가 "다시 시작"을 눌렀을 때 — 재시도 카운터를 되돌린다. */
  async restart(): Promise<void> {
    this.stopping = true;
    this.cancelReadinessWait();
    this.cancelScheduledRestart();
    this.generation += 1;
    return this.enqueue(async () => {
      if (this.disposed) return;
      if (this.managed) await this.stopChild();
      this.state = { ...this.state, restarts: 0 };
      await this.startUnlocked();
    });
  }

  private spawnChild(anthropicApiKey: string, geminiApiKey: string): void {
    const bin = this.options.binary ?? process.env.CODEGATE_SIDECAR_BIN ?? 'codegate-local';
    const runtime = this.runtime;
    if (!runtime) throw new Error('sidecar runtime paths are not configured');

    const child = this.spawnImpl(bin, buildSidecarArgs(this.baseUrl!, this.options.corsOrigin, runtime), {
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: process.platform !== 'win32',
      env: buildSidecarEnvironment(
        process.env,
        this.options.corsOrigin,
        runtime,
        anthropicApiKey,
        geminiApiKey,
        this.options.extraEnv,
      ),
    });

    child.stdout?.on('data', (b: Buffer) => console.info('[sidecar]', b.toString().trimEnd()));
    child.stderr?.on('data', (b: Buffer) => {
      const text = b.toString();
      console.warn('[sidecar]', text.trimEnd());
      // 흘려보내지 않고 꼬리를 남긴다. 기동에 실패했을 때 **왜** 실패했는지가 여기에만 있고,
      // 그걸 알아야 스스로 고칠지(위키 재생성) 그냥 포기할지 판단할 수 있다.
      this.stderrTail = (this.stderrTail + text).slice(-STDERR_TAIL_LIMIT);
    });

    child.on('error', (err) => {
      if (this.child !== child) return;
      logError('sidecar:spawn', err);
      this.spawnError = new Error(
        `엔진 실행 파일을 찾지 못했습니다 (${bin}). 설치 상태를 확인해 주세요.`,
      );
      this.child = null;
      this.setState('crashed', {
        error: this.spawnError.message,
      });
    });

    child.on('exit', (code, signal) => {
      if (this.child !== child) return;
      if (this.stopping) return; // 우리가 내린 것
      this.onAbnormalExit(child, code, signal);
    });

    this.managedTrees.add(child);
    this.child = child;
  }

  private onAbnormalExit(
    child: ChildProcess,
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    if (this.child !== child) return;
    this.child = null;
    const detail = signal ? `신호 ${signal}` : `종료 코드 ${code}`;
    const wasStarting = this.state.status === 'starting';
    let shouldRestart = false;

    // startup 호출자가 health 실패를 정리한다. 여기서 별도 재기동을 예약하면
    // 두 start 흐름이 겹쳐 앱 종료 뒤에도 process가 다시 생길 수 있다.
    if (wasStarting) {
      this.setState('crashed', { error: `엔진 시작 중 종료됐습니다 (${detail}).` });
    } else if (this.state.restarts >= MAX_RESTARTS) {
      this.setState('crashed', {
        error: `엔진이 반복해서 종료됩니다 (${detail}). 설정에서 다시 시작해 주세요.`,
      });
    } else {
      const restarts = this.state.restarts + 1;
      this.setState('restarting', { restarts });
      shouldRestart = true;
    }

    // leader가 먼저 끝나도 같은 process group의 descendant를 모두 정리한 뒤 재시작한다.
    void this.enqueue(async () => {
      try {
        await this.terminateManagedTree(child);
      } catch (error) {
        logError('sidecar:orphan-cleanup', error);
        if (!this.disposed) {
          this.setState('crashed', {
            error: '종료된 엔진의 잔여 process를 정리하지 못했습니다.',
          });
        }
        return;
      }
      if (!shouldRestart || this.stopping || this.disposed || !this.runtime) return;
      // 연달아 죽을 때 즉시 재시도하면 CPU 만 태운다 — 점점 늦춘다
      const generation = this.generation;
      const restarts = this.state.restarts;
      this.cancelScheduledRestart();
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        if (!this.stopping && !this.disposed && this.runtime && generation === this.generation) {
          void this.start();
        }
      }, 500 * restarts);
      this.restartTimer.unref();
    });
  }

  /** `/health` 가 200 을 줄 때까지 짧게 반복해 두드린다. */
  private async waitForHealth(signal: AbortSignal): Promise<void> {
    const configuredTimeout = Number(process.env.CODEGATE_SIDECAR_READY_TIMEOUT_MS);
    const readyTimeoutMs =
      Number.isFinite(configuredTimeout) && configuredTimeout > 0
        ? configuredTimeout
        : DEFAULT_READY_TIMEOUT_MS;
    const deadline = Date.now() + readyTimeoutMs;
    let lastError = '엔진이 응답하지 않습니다.';

    while (Date.now() < deadline) {
      if (signal.aborted) throw new Error('엔진 준비 확인이 취소되었습니다.');
      if (this.spawnError) throw this.spawnError;
      if (this.managed && (!this.child || this.child.exitCode !== null || this.child.signalCode !== null)) {
        throw new Error('엔진 process가 준비되기 전에 종료됐습니다.');
      }
      try {
        const res = await this.fetchImpl(`${this.baseUrl}/api/v1/health`, { signal });
        if (res.ok) {
          const health = (await res.json()) as { status?: unknown; agent_available?: unknown };
          if (health.status === 'ok' && health.agent_available === true) return;
          lastError = '엔진 의존성이 아직 준비되지 않았습니다.';
        } else {
          lastError = `엔진이 준비되지 않았습니다 (HTTP ${res.status}).`;
        }
      } catch {
        if (signal.aborted) throw new Error('엔진 준비 확인이 취소되었습니다.');
        lastError = '엔진에 연결하지 못했습니다.';
      }
      await abortableDelay(HEALTH_INTERVAL_MS, signal);
    }
    throw new Error(lastError);
  }

  private async stopChild(): Promise<void> {
    this.stopping = true;
    this.child = null;
    const trees = [...this.managedTrees];
    for (const tree of trees) await this.terminateManagedTree(tree);
  }

  private async terminateManagedTree(child: ChildProcess): Promise<void> {
    if (!processTreeRunning(child)) {
      this.managedTrees.delete(child);
      return;
    }

    await signalChildTree(child, 'SIGTERM');
    if (await waitForProcessTreeExit(child, KILL_GRACE_MS)) {
      this.managedTrees.delete(child);
      return;
    }
    // 얌전히 안 죽으면 강제로 — 고아로 남기는 것보다 낫다.
    await signalChildTree(child, 'SIGKILL');
    if (!(await waitForProcessTreeExit(child, KILL_GRACE_MS))) {
      throw new Error('엔진 process를 강제 종료한 뒤에도 종료를 확인하지 못했습니다.');
    }
    this.managedTrees.delete(child);
  }

  /** 앱 종료 시. 외부 연결 모드면 남의 프로세스를 건드리지 않는다. */
  async stop(): Promise<void> {
    this.stopping = true;
    this.cancelReadinessWait();
    this.cancelScheduledRestart();
    this.generation += 1;
    return this.enqueue(async () => {
      if (!this.managed) {
        return;
      }
      await this.stopChild();
      this.setState('stopped');
    });
  }

  /** 앱 종료 뒤에는 예약 재시작이나 key 변경이 새 process를 만들 수 없다. */
  async dispose(): Promise<void> {
    this.disposed = true;
    this.stopping = true;
    this.cancelReadinessWait();
    this.cancelScheduledRestart();
    this.generation += 1;
    return this.enqueue(async () => {
      if (this.managed) await this.stopChild();
      this.setState('stopped');
    });
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.lifecycle.then(operation, operation);
    this.lifecycle = next.catch(() => undefined);
    return next;
  }

  private cancelScheduledRestart(): void {
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
  }

  private cancelReadinessWait(): void {
    this.readinessAbort?.abort();
    this.readinessAbort = null;
  }
}

async function prepareRuntimePaths(runtime: SidecarRuntimePaths): Promise<void> {
  await validateSidecarRuntimePaths(runtime);
  await Promise.all(
    [runtime.llmwikiInputRoot, runtime.llmwikiStorageRoot, runtime.dataRoot].map((candidate) =>
      fs.mkdir(candidate, { recursive: true }),
    ),
  );
}

/** 어떤 runtime 디렉터리에도 쓰기 전에 호출하는 read-only 안전성 검증. */
export async function validateSidecarRuntimePaths(runtime: SidecarRuntimePaths): Promise<void> {
  for (const [label, candidate] of [
    ['문서 폴더', runtime.sourceRoot],
    ['LLMWIKI bundle', runtime.llmwikiProjectRoot],
  ] as const) {
    const stat = await fs.stat(candidate).catch(() => null);
    if (!stat?.isDirectory()) throw new Error(`${label}를 찾을 수 없습니다: ${candidate}`);
  }

  const roots = await Promise.all(
    [
      runtime.sourceRoot,
      runtime.llmwikiProjectRoot,
      runtime.llmwikiInputRoot,
      runtime.llmwikiStorageRoot,
      runtime.dataRoot,
    ].map(resolvePhysicalPath),
  );
  for (const [index, left] of roots.entries()) {
    for (const right of roots.slice(index + 1)) {
      if (left === right || left.startsWith(`${right}${path.sep}`) || right.startsWith(`${left}${path.sep}`)) {
        throw new Error('sidecar source, bundle, input, storage, data 경로는 서로 겹칠 수 없습니다.');
      }
    }
  }

}

async function resolvePhysicalPath(candidate: string): Promise<string> {
  let current = path.resolve(candidate);
  const missing: string[] = [];
  while (true) {
    try {
      const physical = await fs.realpath(current);
      return path.join(physical, ...missing.reverse());
    } catch {
      const parent = path.dirname(current);
      if (parent === current) return path.resolve(candidate);
      missing.push(path.basename(current));
      current = parent;
    }
  }
}

async function waitForProcessTreeExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!processTreeRunning(child)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return !processTreeRunning(child);
}

function abortableDelay(timeoutMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new Error('엔진 준비 확인이 취소되었습니다.'));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, timeoutMs);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error('엔진 준비 확인이 취소되었습니다.'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function processTreeRunning(child: ChildProcess): boolean {
  if (process.platform !== 'win32' && child.pid) {
    try {
      process.kill(-child.pid, 0);
      return true;
    } catch (error) {
      return (error as NodeJS.ErrnoException).code !== 'ESRCH';
    }
  }
  return child.exitCode === null && child.signalCode === null;
}

async function signalChildTree(child: ChildProcess, signal: NodeJS.Signals): Promise<void> {
  if (!child.pid) {
    child.kill(signal);
    return;
  }
  if (process.platform !== 'win32') {
    try {
      process.kill(-child.pid, signal);
    } catch {
      child.kill(signal);
    }
    return;
  }
  const args = ['/pid', String(child.pid), '/t'];
  if (signal === 'SIGKILL') args.push('/f');
  const succeeded = await new Promise<boolean>((resolve) => {
    const killer = spawn('taskkill.exe', args, { stdio: 'ignore' });
    killer.once('error', () => resolve(false));
    killer.once('exit', (code) => resolve(code === 0));
  });
  if (!succeeded && child.exitCode === null && child.signalCode === null) child.kill(signal);
}
