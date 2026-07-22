import type { CodegateApi } from './api';

declare global {
  interface Window {
    codegate: CodegateApi;
  }
}

export {};
