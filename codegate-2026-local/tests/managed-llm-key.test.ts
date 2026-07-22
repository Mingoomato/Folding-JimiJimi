import { describe, expect, it } from 'vitest';
import { ManagedLlmKeyStore, readInternalApiKey } from '@main/auth/internal-key';
import { MemoryLlmKeyStore } from '@main/auth/llm-key';

/**
 * 문서 분석 키는 **구독에 포함**된다 — 사용자는 발급받지도 입력하지도 않는다.
 *
 * 여기서 지키는 약속은 세 가지다.
 *   · 내부 키가 있으면 언제나 내부 키가 이긴다 (사용자 키가 남아 있어도)
 *   · 내부 키가 없으면 예전처럼 사용자 키로 폴백한다 — 기존 설치본이 죽지 않는다
 *   · 원문 키는 상태로 새어 나가지 않는다 — 끝 4자리만
 */
describe('readInternalApiKey', () => {
  it('환경변수를 정해진 우선순위로 읽는다', () => {
    expect(
      readInternalApiKey({ ANTHROPIC_API_KEY: 'sk-env', CODEGATE_ANTHROPIC_API_KEY: 'sk-codegate' }),
    ).toBe('sk-codegate');
    expect(readInternalApiKey({ CODEGATE_LLM_API_KEY: 'sk-llm' })).toBe('sk-llm');
  });

  it('빈 값이나 공백은 키로 치지 않는다 — 있는 척하면 실패 원인을 못 찾는다', () => {
    expect(readInternalApiKey({ ANTHROPIC_API_KEY: '   ' })).toBeNull();
    expect(readInternalApiKey({})).toBeNull();
  });
});

describe('ManagedLlmKeyStore', () => {
  it('내부 키가 있으면 사용자 키보다 우선한다', async () => {
    const inner = new MemoryLlmKeyStore();
    await inner.set({ provider: 'anthropic', apiKey: 'sk-user' });
    const store = new ManagedLlmKeyStore(inner, 'sk-internal');

    expect(await store.get('anthropic')).toBe('sk-internal');
    expect(await store.resolve()).toEqual({ provider: 'anthropic', apiKey: 'sk-internal' });
  });

  it('사용자가 넣어 둔 다른 제공자 키가 있어도 내부 키로 답한다', async () => {
    const inner = new MemoryLlmKeyStore();
    await inner.set({ provider: 'gemini', apiKey: 'g-user' });
    const store = new ManagedLlmKeyStore(inner, 'sk-internal');

    expect(await store.resolve()).toEqual({ provider: 'anthropic', apiKey: 'sk-internal' });
  });

  it('내부 키가 없으면 사용자 키로 폴백한다', async () => {
    const inner = new MemoryLlmKeyStore();
    await inner.set({ provider: 'gemini', apiKey: 'g-user' });
    const store = new ManagedLlmKeyStore(inner, null);

    expect(await store.resolve()).toEqual({ provider: 'gemini', apiKey: 'g-user' });
    expect(store.managed).toBe(false);
  });

  it('상태에는 managed 표시와 끝 4자리만 나간다 — 원문 키는 절대 나가지 않는다', async () => {
    const store = new ManagedLlmKeyStore(new MemoryLlmKeyStore(), 'sk-ant-secret-9f2c');
    const anthropic = (await store.status()).find((s) => s.provider === 'anthropic');

    expect(anthropic).toEqual({ provider: 'anthropic', configured: true, hint: '…9f2c', managed: true });
    expect(JSON.stringify(await store.status())).not.toContain('secret');
  });

  it('내부 키로 도는 동안 사용자가 같은 제공자 키를 저장하려 하면 거부한다', async () => {
    const store = new ManagedLlmKeyStore(new MemoryLlmKeyStore(), 'sk-internal');
    // 조용히 저장해 두면 "저장했는데 안 쓰인다"가 된다.
    await expect(store.set({ provider: 'anthropic', apiKey: 'sk-user' })).rejects.toThrow(/구독/);
  });

  it('내부 키가 없으면 사용자 저장은 그대로 동작한다', async () => {
    const store = new ManagedLlmKeyStore(new MemoryLlmKeyStore(), null);
    const status = await store.set({ provider: 'anthropic', apiKey: 'sk-user-abcd' });

    expect(status.find((s) => s.provider === 'anthropic')).toEqual({
      provider: 'anthropic',
      configured: true,
      hint: '…abcd',
    });
  });
});
