import { createServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';
import type { AgentDeps } from '@contracts';
import {
  createOAuthLoopbackReceiver,
  DEFAULT_OAUTH_REDIRECT_URI,
  OAuthCancelledError,
} from '@main/auth/oauth-loopback';
import { oauthStateMatches } from '@main/auth';
import { createSidecarAgent } from '@main/agent/sidecar-agent';
import { MIGRATIONS } from '@main/db/schema';

const servers: Server[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map(close));
});

describe('sidecar execution persistence', () => {
  // 마이그레이션은 배열 끝에만 추가하는 규칙이므로, "마지막 항목"을 고정하면 다음 단계를
  // 추가할 때마다 깨진다. 검증할 것은 위치가 아니라 **그 단계가 존재하는지** 다.
  it('adds an execution column in a SQLite migration', () => {
    expect(MIGRATIONS.join('\n')).toContain('ALTER TABLE messages ADD COLUMN execution TEXT');
  });

  it('keeps migrations append-only so existing installs replay in the same order', () => {
    expect(MIGRATIONS[2]).toContain('ALTER TABLE messages ADD COLUMN execution TEXT');
  });
});

describe('OAuth loopback receiver', () => {
  // 포트 0 = 임시 포트. 실제 앱은 콘솔에 등록된 고정 포트를 쓴다.
  const EPHEMERAL = 'http://127.0.0.1:0/auth/callback';

  it('rejects the wrong path and accepts the pending callback once', async () => {
    const receiver = await createOAuthLoopbackReceiver(2_000, EPHEMERAL);
    try {
      const callback = new URL(receiver.redirectTo);
      const wrong = new URL(callback);
      wrong.pathname = '/oauth/callback/attacker';
      wrong.searchParams.set('code', 'stolen-code');
      expect((await fetch(wrong)).status).toBe(404);

      callback.searchParams.set('code', 'valid-code');
      callback.searchParams.set('state', 'flow-state-1');
      expect((await fetch(callback)).status).toBe(200);
      // 경로는 이제 고정이므로, "우리가 시작한 흐름인가"는 state 대조가 전담한다.
      await expect(receiver.waitForCode()).resolves.toEqual({
        code: 'valid-code',
        state: 'flow-state-1',
      });
    } finally {
      await receiver.close();
    }
  });

  /**
   * Google Console 에 등록한 주소와 **글자 그대로** 같아야 한다.
   * 경로가 한 글자라도 다르면 Web application client에서 400 redirect_uri_mismatch 가 난다.
   */
  it('serves exactly the registered callback path', async () => {
    const receiver = await createOAuthLoopbackReceiver(2_000, EPHEMERAL);
    try {
      expect(new URL(receiver.redirectTo).pathname).toBe('/auth/callback');
      expect(receiver.redirectTo).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/auth\/callback$/);
    } finally {
      await receiver.close();
    }
  });

  it('기본값은 콘솔에 등록된 고정 주소다', () => {
    expect(DEFAULT_OAUTH_REDIRECT_URI).toBe('http://127.0.0.1:47821/auth/callback');
  });

  it('state가 빠진 콜백은 로그인 흐름과 일치하지 않는 것으로 거부한다', () => {
    expect(oauthStateMatches(null, 'expected-state')).toBe(false);
    expect(oauthStateMatches('', 'expected-state')).toBe(false);
  });

  it('다른 state는 길이가 같거나 달라도 거부하고 정확한 값만 받는다', () => {
    expect(oauthStateMatches('attacker-state', 'expected-state')).toBe(false);
    expect(oauthStateMatches('x', 'expected-state')).toBe(false);
    expect(oauthStateMatches('expected-state', 'expected-state')).toBe(true);
  });

  /**
   * 콜백 포트는 콘솔에 등록된 **고정값**이라 하나뿐이다. 로그인을 띄웠다 그냥 닫으면
   * 이전 시도가 포트를 붙든 채 남아, 다시 누를 때 EADDRINUSE 로 막혔다.
   * close() 는 기다리던 약속을 끊고 포트를 반드시 돌려줘야 한다.
   */
  it('버려진 로그인을 닫으면 같은 포트를 곧바로 다시 쓸 수 있다', async () => {
    const FIXED = 'http://127.0.0.1:47899/auth/callback';
    const first = await createOAuthLoopbackReceiver(60_000, FIXED);
    const pending = first.waitForCode();

    await first.close();
    // 기다리던 쪽이 영원히 잠들지 않고 "취소"로 깨어난다.
    await expect(pending).rejects.toBeInstanceOf(OAuthCancelledError);

    // 같은 포트를 즉시 다시 잡을 수 있어야 한다 — 이게 안 되면 두 번째 로그인이 막힌다.
    const second = await createOAuthLoopbackReceiver(2_000, FIXED);
    expect(second.redirectTo).toBe(FIXED);
    await second.close();
  });
});

describe('Python sidecar agent bridge', () => {
  it('sends no bearer token and approves the exact plan hash', async () => {
    const requests: { path: string; authorization?: string; idempotency?: string; body: unknown }[] = [];
    const server = createServer(async (request, response) => {
      const body = await readJson(request);
      requests.push({
        path: request.url ?? '',
        authorization: request.headers.authorization,
        idempotency: request.headers['idempotency-key'] as string | undefined,
        body,
      });
      response.setHeader('content-type', 'application/json');
      if (request.url === '/api/v2/chat/messages') {
        response.end(
          JSON.stringify({
            conversation_id: 'conversation-1',
            message_id: 'message-1',
            response_type: 'change_preview',
            assistant_text: '변경안을 확인해 주세요.',
            documents: [
              {
                document_id: 'REG-000001',
                revision: '3',
                display_path: 'regulations/REG-000001.md',
                source_uri: 'source://regulations/REG-000001.md',
                graph_version: 'build-0123456789abcdefabcd',
                can_write: true,
                evidence: [
                  {
                    chunk_id: 'REG-000001@3#sec-004',
                    section_id: 'sec-004',
                    section: '제4조',
                    heading_path: ['규정', '제4조'],
                    quote: '5년간 보관한다.',
                  },
                ],
              },
            ],
            change_plan: {
              change_plan_id: 'plan-1',
              document_id: 'REG-000001',
              source_uri: 'source://regulations/REG-000001.md',
              unified_diff: '-5년\n+3년',
              plan_hash: 'a'.repeat(64),
            },
            document_plan: null,
            error: null,
          }),
        );
        return;
      }
      if (request.url === '/api/v1/change-plans/plan-1/approve') {
        response.statusCode = 202;
        response.end(
          JSON.stringify({
            execution_id: 'execution-1',
            document_id: 'REG-000001',
            status: 'completed',
            sync_status: 'published',
            terminal: true,
            stage: 'published',
            recommended_poll_after_ms: null,
            error: null,
          }),
        );
        return;
      }
      response.statusCode = 404;
      response.end('{}');
    });
    const baseUrl = await listen(server);
    servers.push(server);

    const approvals: string[] = [];
    const deps: AgentDeps = {
      config: { roots: [], backendUrl: `${baseUrl}/api/v1`, wikiDir: '/tmp/wiki' },
      auth: { getToken: async () => 'must-not-be-used', canWrite: () => true },
      approvalHandler: async (request) => {
        approvals.push(request.planHash ?? '');
        return true;
      },
      kordoc: {
        parse: async () => '',
        patch: async () => ({
          ok: true,
          exitCode: 0,
          applied: 0,
          unapplied: [],
          backupPath: '',
        }),
        generate: async () => undefined,
        render: async () => [],
      },
    };
    const events = [];
    for await (const event of createSidecarAgent(deps).send('5년을 3년으로 바꿔줘', {
      conversationId: 'conversation-1',
    })) {
      events.push(event);
    }

    expect(approvals).toEqual(['a'.repeat(64)]);
    expect(requests.map((request) => request.authorization)).toEqual([undefined, undefined]);
    expect(requests.every((request) => Boolean(request.idempotency))).toBe(true);
    expect(requests[1]?.body).toEqual({ plan_hash: 'a'.repeat(64) });
    expect(events.at(-1)).toMatchObject({
      type: 'done',
      citations: [
        {
          docId: 'REG-000001',
          rev: '3',
          graphVersion: 'build-0123456789abcdefabcd',
          chunkId: 'REG-000001@3#sec-004',
          sectionId: 'sec-004',
        },
      ],
    });
  });

  it('routes native document proposals through v2 typed approval', async () => {
    const paths: string[] = [];
    const server = createServer(async (request, response) => {
      await readJson(request);
      paths.push(request.url ?? '');
      response.setHeader('content-type', 'application/json');
      if (request.url === '/api/v2/chat/messages') {
        response.end(
          JSON.stringify({
            conversation_id: 'native-conversation',
            message_id: 'native-message',
            response_type: 'change_preview',
            assistant_text: 'Native document preview is ready.',
            documents: [],
            change_plan: null,
            document_plan: {
              change_plan_id: 'dplan-1',
              kind: 'mutation',
              document_id: 'DOC-000001',
              format: 'docx',
              capability_id: 'docx.writer.python-docx/v1',
              source_uri: 'source://reports/report.docx',
              target_relative_path: null,
              status: 'pending_approval',
              base_sha256: 'a'.repeat(64),
              proposed_sha256: 'b'.repeat(64),
              plan_hash: 'c'.repeat(64),
              graph_version: 'graph-1',
              capability_snapshot_id: 'd'.repeat(64),
              writer_fingerprint: 'python-docx/1.2.0',
              renderer_fingerprint: 'LibreOffice/test',
              operations: [],
              structural_diff: [
                {
                  operation_index: 0,
                  operation_type: 'text.replace/v1',
                  locator: { kind: 'paragraph', block_index: 1 },
                  before: 'old',
                  after: 'new',
                },
              ],
              preview_manifest: {
                manifest_sha256: 'e'.repeat(64),
                pairs: [
                  {
                    locator_label: 'page-1',
                    before_artifact_id: null,
                    after_artifact_id: null,
                    summary_only: true,
                  },
                ],
                truncated_count: 0,
              },
              warnings: [],
              error: null,
            },
            error: null,
          }),
        );
        return;
      }
      if (request.url === '/api/v2/change-plans/dplan-1/approve') {
        response.statusCode = 202;
        response.end(
          JSON.stringify({
            execution_id: 'exec-1',
            change_plan_id: 'dplan-1',
            undo_of_execution_id: null,
            document_id: 'DOC-000001',
            change_kind: 'update',
            format: 'docx',
            capability_id: 'docx.writer.python-docx/v1',
            source_uri: 'source://reports/report.docx',
            status: 'completed',
            before_sha256: 'a'.repeat(64),
            after_sha256: 'b'.repeat(64),
            artifact_sha256: 'b'.repeat(64),
            graph_version_before: 'graph-1',
            graph_version_after: 'graph-2',
            error: null,
          }),
        );
        return;
      }
      response.statusCode = 404;
      response.end('{}');
    });
    const baseUrl = await listen(server);
    servers.push(server);
    let approvalPreview: unknown;
    const deps: AgentDeps = {
      config: { roots: [], backendUrl: `${baseUrl}/api/v1`, wikiDir: '/tmp/wiki' },
      auth: { getToken: async () => '', canWrite: () => true },
      approvalHandler: async (request) => {
        approvalPreview = request.documentPreview;
        return true;
      },
      kordoc: {
        parse: async () => '',
        patch: async () => ({
          ok: true,
          exitCode: 0,
          applied: 0,
          unapplied: [],
          backupPath: '',
        }),
        generate: async () => undefined,
        render: async () => [],
      },
    };
    const events = [];
    for await (const event of createSidecarAgent(deps).send('replace old with new', {
      conversationId: 'native-conversation',
    })) {
      events.push(event);
    }

    expect(paths).toEqual([
      '/api/v2/chat/messages',
      '/api/v2/change-plans/dplan-1/approve',
    ]);
    expect(approvalPreview).toMatchObject({
      format: 'docx',
      capabilityId: 'docx.writer.python-docx/v1',
      targetRelativePath: 'reports/report.docx',
    });
    expect(events.at(-1)).toMatchObject({
      type: 'done',
      execution: { executionId: 'exec-1', status: 'completed', stage: 'published' },
    });
  });
});

function listen(server: Server): Promise<string> {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      const address = server.address() as AddressInfo;
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

function close(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  if (chunks.length === 0) return null;
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
}
