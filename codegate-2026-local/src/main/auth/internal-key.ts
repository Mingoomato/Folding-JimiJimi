/**
 * 앱이 **내부적으로** 쥐는 LLM 키 (구독 모델).
 *
 * 사용자는 키를 발급받지도, 입력하지도 않는다. 요금은 구독으로 받고 토큰은 우리가 낸다.
 * 그래서 온보딩의 "키 설정" 단계는 사라졌고, 키는 이 계층이 공급한다.
 *
 * 공급 경로는 환경변수 하나뿐이다:
 *   `CODEGATE_ANTHROPIC_API_KEY` · `ANTHROPIC_API_KEY` · `CODEGATE_LLM_API_KEY`
 *
 * ⚠️ 정직하게 적어 둔다 — **데스크톱 앱에 심는 키는 비밀이 될 수 없다.**
 * 설치본에 넣는 순간 누구나 꺼낼 수 있고, 꺼내면 우리 요금으로 쓴다. 그래서 이 계층은
 * 개발·시연용 배선이고, 제품 경로는 백엔드가 대신 호출하는 프록시다
 * (doc2md·OAuth 를 올리는 그 백엔드에 `/llm` 을 하나 더 두는 형태).
 * 그때 이 파일은 "환경변수에서 읽기" 대신 "백엔드에서 단기 토큰 받기"로 바뀐다.
 *
 * 사용자가 직접 넣어 둔 키(`llm-keys.bin`)는 지우지 않는다. 내부 키가 없을 때의
 * 폴백으로만 남긴다 — 내부 키가 있으면 언제나 내부 키가 이긴다.
 */
import type { LlmKeyInput, LlmKeyStatus, LlmProvider } from '@contracts';
import { UserFacingError } from '@main/util/errors';
import type { ResolvedLlmKey } from '@main/build/types';
import { hintOf, type LlmKeyStore } from './llm-key';

/** 내부 키를 찾는 환경변수 — 앞에 있는 것이 이긴다. */
export const INTERNAL_KEY_ENV = [
  'CODEGATE_ANTHROPIC_API_KEY',
  'ANTHROPIC_API_KEY',
  'CODEGATE_LLM_API_KEY',
] as const;

/**
 * 내부 키의 기본 제공자.
 *
 * sidecar agent와 위키 enrichment는 서로 다른 provider를 선택할 수 있다. 그래서 제공자별로
 * 따로 읽는다. 하나로 뭉치면 "키를 넣었는데 답변이나 간선이 안 생긴다"가 된다.
 */
const INTERNAL_PROVIDER: LlmProvider = 'anthropic';

/** 제공자별 환경변수. 앞에 있는 것이 이긴다. */
const PROVIDER_ENV: Record<LlmProvider, readonly string[]> = {
  anthropic: INTERNAL_KEY_ENV,
  gemini: ['CODEGATE_GEMINI_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY'],
  openai: ['CODEGATE_OPENAI_API_KEY', 'OPENAI_API_KEY'],
};

function readEnvKey(env: NodeJS.ProcessEnv, names: readonly string[]): string | null {
  for (const name of names) {
    const value = env[name]?.trim();
    if (value) return value;
  }
  return null;
}

export function readInternalApiKey(env: NodeJS.ProcessEnv): string | null {
  return readEnvKey(env, INTERNAL_KEY_ENV);
}

/** 제공자별 내부 키. 위키 enrichment 처럼 Anthropic 이 아닌 경로가 쓴다. */
export function readInternalKeyFor(env: NodeJS.ProcessEnv, provider: LlmProvider): string | null {
  return readEnvKey(env, PROVIDER_ENV[provider] ?? []);
}

/**
 * 내부 키를 사용자 저장소 **위에** 얹는다.
 *
 * `LlmKeyStore` 를 그대로 구현하므로 호출부(빌드 파이프라인·sidecar supervisor·IPC)는
 * 내부 키의 존재를 모른다. 바뀐 것은 "키가 어디서 오는가" 하나뿐이다.
 */
export class ManagedLlmKeyStore implements LlmKeyStore {
  constructor(
    private readonly inner: LlmKeyStore,
    private readonly internalKey: string | null,
  ) {}

  /** 내부 키로 돌고 있는가 — 설정 화면이 키 입력폼을 감출지 정할 때 쓴다. */
  get managed(): boolean {
    return this.internalKey !== null;
  }

  async status(): Promise<LlmKeyStatus[]> {
    const inner = await this.inner.status();
    if (!this.internalKey) return inner;
    return inner.map((status) =>
      status.provider === INTERNAL_PROVIDER
        ? { provider: status.provider, configured: true, hint: hintOf(this.internalKey!), managed: true }
        : status,
    );
  }

  async set(input: LlmKeyInput): Promise<LlmKeyStatus[]> {
    if (this.internalKey && input.provider === INTERNAL_PROVIDER) {
      // 조용히 저장해 두면 "저장했는데 안 쓰인다"가 된다. 안 쓴다고 말한다.
      throw new UserFacingError('문서 분석 키는 구독에 포함되어 있어 따로 등록하지 않습니다.');
    }
    await this.inner.set(input);
    return this.status();
  }

  async clear(provider: LlmProvider): Promise<LlmKeyStatus[]> {
    await this.inner.clear(provider);
    return this.status();
  }

  async get(provider: LlmProvider): Promise<string | null> {
    if (this.internalKey && provider === INTERNAL_PROVIDER) return this.internalKey;
    // Anthropic 외 제공자도 환경변수를 먼저 본다 — 위키 enrichment 는 Gemini 를 쓴다.
    return readInternalKeyFor(process.env, provider) ?? this.inner.get(provider);
  }

  async resolve(): Promise<ResolvedLlmKey | null> {
    if (this.internalKey) return { provider: INTERNAL_PROVIDER, apiKey: this.internalKey };
    return this.inner.resolve();
  }
}
