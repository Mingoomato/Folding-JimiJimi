import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

// 메인 프로세스 모듈을 node 환경에서 그대로 돌린다 (electron 을 import 하는 모듈은 테스트 대상이 아니다).
export default defineConfig({
  resolve: {
    alias: {
      '@contracts': resolve('packages/contracts/src/index.ts'),
      '@main': resolve('src/main'),
      // 렌더러의 **순수 로직**만 테스트한다 (JSX 는 node 환경에서 못 돈다).
      '@renderer': resolve('src/renderer/src'),
    },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
});
