#!/usr/bin/env node
/**
 * 목 서버 (스펙 v1.4 §2).
 *
 * v1.4 에서 서버의 역할은 **로그인·구독 확인뿐**이다.
 * v1.3 의 `POST /builds` · `GET /builds/{id}` · `/artifact` · `/ack` 는 삭제됐다 —
 * 변환·enrichment·조립이 전부 사용자 컴퓨터에서 돌기 때문에 문서가 서버로 올라가지 않는다.
 *
 * 외부 패키지 없이 node:http 만 쓴다.  실행: `pnpm mock-server`
 *
 *   POST /auth/login       → { token, email }
 *   GET  /me/subscription  → { plan, expiresAt, active }
 *
 * 인증 UX 를 시험하기 위한 환경변수:
 *   MOCK_LOGIN_401=1           로그인이 401 로 실패
 *   MOCK_UNAUTHENTICATED=1     토큰이 필요한 모든 요청이 401
 *   MOCK_SUBSCRIPTION=expired  구독 조회가 402
 *   MOCK_PORT=8787
 */
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';

const PORT = Number(process.env.MOCK_PORT ?? 8787);
const LOGIN_401 = process.env.MOCK_LOGIN_401 === '1';
const UNAUTHENTICATED = process.env.MOCK_UNAUTHENTICATED === '1';
const SUBSCRIPTION_EXPIRED = process.env.MOCK_SUBSCRIPTION === 'expired';

function send(res, status, body) {
  const payload = body === undefined ? '' : JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

function hasBearer(req) {
  const auth = req.headers.authorization;
  return typeof auth === 'string' && auth.startsWith('Bearer ');
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    return {};
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`);
  const route = `${req.method} ${url.pathname}`;

  try {
    if (route === 'POST /auth/login') {
      if (LOGIN_401) {
        return send(res, 401, { message: '이메일 또는 비밀번호가 올바르지 않습니다.' });
      }
      const body = await readJson(req);
      return send(res, 200, {
        token: `mock-token-${randomUUID()}`,
        email: body.email || 'woochang@codegate.dev',
      });
    }

    if (route === 'GET /me/subscription') {
      if (UNAUTHENTICATED || !hasBearer(req)) {
        return send(res, 401, { message: '로그인이 필요합니다.' });
      }
      if (SUBSCRIPTION_EXPIRED) {
        return send(res, 402, { message: '구독이 만료되었습니다.' });
      }
      return send(res, 200, {
        plan: 'Pro',
        expiresAt: '2026-12-31T23:59:59.000Z',
        active: true,
      });
    }

    // v1.4 에서 사라진 경로를 누가 아직 부른다면, 조용히 404 내지 말고 이유를 알려준다.
    if (url.pathname.startsWith('/builds')) {
      return send(res, 410, {
        message:
          '빌드 API 는 스펙 v1.4 에서 삭제되었습니다. 빌드는 로컬에서 수행됩니다.',
      });
    }

    return send(res, 404, { message: `알 수 없는 경로: ${route}` });
  } catch (err) {
    return send(res, 500, { message: err instanceof Error ? err.message : '서버 오류' });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`목 서버 실행 중 → http://127.0.0.1:${PORT}`);
  console.log('  POST /auth/login · GET /me/subscription  (서버 API는 이 둘이 전부)');
  if (LOGIN_401) console.log('  · MOCK_LOGIN_401=1 — 로그인이 401 로 실패합니다');
  if (UNAUTHENTICATED) console.log('  · MOCK_UNAUTHENTICATED=1 — 인증 요청이 401 로 실패합니다');
  if (SUBSCRIPTION_EXPIRED) console.log('  · MOCK_SUBSCRIPTION=expired — 구독 조회가 402 입니다');
});
