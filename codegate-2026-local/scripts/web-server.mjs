#!/usr/bin/env node
import { createReadStream, existsSync } from 'node:fs';
import { stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = fileURLToPath(new URL('.', import.meta.url));
const publicDir = resolve(scriptDir, '..', 'dist', 'web');
const indexPath = join(publicDir, 'index.html');
const port = Number(process.env.PORT ?? 4173);

if (!existsSync(indexPath)) {
  console.error('웹 빌드가 없습니다. pnpm build:web을 먼저 실행해 주세요.');
  process.exit(1);
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? 'localhost'}`);
  setSecurityHeaders(response);

  if (url.pathname === '/health') {
    response.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify({ status: 'ok', runtime: 'web-demo' }));
    return;
  }

  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { allow: 'GET, HEAD' });
    response.end();
    return;
  }

  try {
    const requested = safePath(url.pathname);
    const filePath = requested && (await isFile(requested)) ? requested : indexPath;
    const fileStat = await stat(filePath);
    response.writeHead(200, {
      'content-type': contentType(filePath),
      'content-length': fileStat.size,
      'cache-control': filePath.includes(`${join('assets', '')}`)
        ? 'public, max-age=31536000, immutable'
        : 'no-cache',
    });
    if (request.method === 'HEAD') response.end();
    else createReadStream(filePath).pipe(response);
  } catch (error) {
    console.error('web request failed', error instanceof Error ? error.message : error);
    response.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
    response.end('웹 페이지를 불러오지 못했습니다.');
  }
});

server.listen(port, '0.0.0.0', () => {
  console.log(`CODEGATE 웹 프런트 실행 중: 0.0.0.0:${port}`);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}

function safePath(pathname) {
  const decoded = decodeURIComponent(pathname);
  const relative = normalize(decoded).replace(/^[/\\]+/, '');
  const candidate = resolve(publicDir, relative);
  if (candidate !== publicDir && !candidate.startsWith(`${publicDir}/`)) return null;
  return candidate;
}

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

function setSecurityHeaders(response) {
  response.setHeader('content-security-policy', [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join('; '));
  response.setHeader('referrer-policy', 'no-referrer');
  response.setHeader('x-content-type-options', 'nosniff');
  response.setHeader('x-frame-options', 'DENY');
  response.setHeader('permissions-policy', 'camera=(), microphone=(), geolocation=()');
}

function contentType(path) {
  return {
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.woff2': 'font/woff2',
  }[extname(path).toLowerCase()] ?? 'application/octet-stream';
}
