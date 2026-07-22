import { describe, expect, it } from 'vitest';
// Production launcher는 Node가 직접 실행하는 작은 ESM helper다.
// @ts-expect-error JavaScript helper에는 별도 declaration 파일을 만들지 않는다.
import { safeProcessEnvironment } from '../scripts/safe-process-environment.mjs';

describe('dev:integrated doc2md environment boundary', () => {
  it('DOC2MD_DATA_ROOT는 유지하고 cloud/LLM 비밀은 넘기지 않는다', () => {
    const safe = safeProcessEnvironment({
      PATH: '/usr/bin',
      DOC2MD_DATA_ROOT: '/workspace/.data/doc2md',
      CODEGATE_GOOGLE_CLIENT_SECRET: 'must-not-cross',
      GEMINI_API_KEY: 'must-not-cross',
    });

    expect(safe).toEqual({
      PATH: '/usr/bin',
      DOC2MD_DATA_ROOT: '/workspace/.data/doc2md',
    });
  });
});
