import { resolve } from 'node:path';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  root: resolve('src/renderer'),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@contracts': resolve('packages/contracts/src/index.ts'),
      '@': resolve('src/renderer/src'),
    },
  },
  build: {
    outDir: resolve('dist/web'),
    emptyOutDir: true,
    rollupOptions: {
      input: { index: resolve('src/renderer/index.html') },
    },
  },
});
