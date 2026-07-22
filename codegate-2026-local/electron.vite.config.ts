import { resolve } from 'node:path';
import { defineConfig, externalizeDepsPlugin } from 'electron-vite';
import type { PluginOption } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * 배포본 CSP.
 * 렌더러는 바깥과 직접 통신하지 않는다 — 백엔드 호출은 전부 메인 프로세스가 한다.
 * 그래서 connect-src 까지 'self' 로 잠글 수 있다.
 * 폰트·이미지도 앱에 번들되므로 원격 출처가 필요 없다.
 */
const PROD_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  // 컴포넌트의 style= 속성 때문에 인라인 '스타일'만 허용한다 (스크립트는 아님)
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ');

/**
 * CSP는 **빌드 산출물에만** 넣는다.
 *
 * 개발 중에는 @vitejs/plugin-react 가 react-refresh 프리앰블을 인라인 스크립트로
 * 주입하는데, 위 정책의 `script-src 'self'` 가 그것을 막아 렌더러가 통째로 죽는다
 * (창은 뜨지만 하얀 화면). 그렇다고 'unsafe-inline' 을 넣으면 배포본까지 약해진다.
 * 개발 서버는 localhost 만 바라보므로 개발 중에는 생략한다.
 */
function cspPlugin(): PluginOption {
  return {
    name: 'codegate:csp',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: PROD_CSP },
          injectTo: 'head-prepend',
        },
      ];
    },
  };
}

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    resolve: {
      alias: {
        '@contracts': resolve('packages/contracts/src/index.ts'),
        '@main': resolve('src/main'),
      },
    },
    build: {
      rollupOptions: {
        input: { index: resolve('src/main/index.ts') },
      },
    },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    resolve: {
      alias: {
        '@contracts': resolve('packages/contracts/src/index.ts'),
      },
    },
    build: {
      rollupOptions: {
        input: { index: resolve('src/preload/index.ts') },
      },
    },
  },
  renderer: {
    root: resolve('src/renderer'),
    plugins: [react(), tailwindcss(), cspPlugin()],
    resolve: {
      alias: {
        '@contracts': resolve('packages/contracts/src/index.ts'),
        '@': resolve('src/renderer/src'),
      },
    },
    build: {
      rollupOptions: {
        input: { index: resolve('src/renderer/index.html') },
      },
    },
  },
});
