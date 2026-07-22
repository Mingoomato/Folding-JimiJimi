/**
 * 자격증명 ② — enrichment LLM 키 저장소 (스펙 v1.4 §5 "자격증명 3종 구분").
 *
 *   ① 백엔드 토큰(로그인)          — safeStorage, `auth.bin`
 *   ② enrichment LLM 키(Gemini 등) — safeStorage, `llm-keys.bin`   ← 이 파일
 *   ③ Anthropic SDK 자격증명       — 환경변수 전용, 절대 저장하지 않는다
 *
 * 셋은 **파일부터 분리**한다. ①과 같은 blob 에 섞이면 로그아웃 한 번에 ②가 날아가고,
 * 반대로 ②를 지우려다 ①이 딸려 나간다. 그래서 저장소를 통째로 따로 둔다.
 *
 * 그리고 원문 키는 **절대 렌더러로 내려보내지 않는다** — `LlmKeyStatus` 는 설정 여부와
 * 끝 4자리(hint)만 담는다 (계약 §5).
 */
import path from 'node:path';
import fs from 'node:fs/promises';
import { safeStorage } from 'electron';
import {
  LLM_PROVIDER_LABEL,
  type LlmKeyInput,
  type LlmKeyStatus,
  type LlmProvider,
} from '@contracts';
import { UserFacingError, logError } from '@main/util/errors';
import { writeFileAtomic } from '@main/util/fsx';
import type { LlmKeyResolver, ResolvedLlmKey } from '@main/build/types';

/** 계약의 `LlmProvider` 전부. 상태 조회는 항상 이 순서로 3개를 돌려준다. */
export const LLM_PROVIDERS = Object.keys(LLM_PROVIDER_LABEL) as LlmProvider[];

/** ①(auth.bin)과 절대 같은 파일을 쓰지 않는다. */
export const LLM_KEY_FILE = 'llm-keys.bin';

type KeyMap = Partial<Record<LlmProvider, string>>;

export interface LlmKeyStore extends LlmKeyResolver {
  status(): Promise<LlmKeyStatus[]>;
  set(input: LlmKeyInput): Promise<LlmKeyStatus[]>;
  clear(provider: LlmProvider): Promise<LlmKeyStatus[]>;
  get(provider: LlmProvider): Promise<string | null>;
}

/** 끝 4자리만 노출한다 (`…8f2a`). 그 외의 어떤 조각도 렌더러로 가지 않는다. */
export function hintOf(apiKey: string): string {
  return `…${apiKey.slice(-4)}`;
}

function toStatus(keys: KeyMap): LlmKeyStatus[] {
  return LLM_PROVIDERS.map((provider) => {
    const apiKey = keys[provider];
    return apiKey
      ? { provider, configured: true, hint: hintOf(apiKey) }
      : { provider, configured: false };
  });
}

/** 어떤 provider 를 enrichment 에 쓸지 — 설정된 것 중 이 우선순위로 고른다. */
const PREFERENCE: LlmProvider[] = ['gemini', 'openai', 'anthropic'];

function resolveFrom(keys: KeyMap): ResolvedLlmKey | null {
  for (const provider of PREFERENCE) {
    const apiKey = keys[provider];
    if (apiKey) return { provider, apiKey };
  }
  return null;
}

/** 암호화가 불가능한 환경(리눅스 키링 부재 등)에서는 메모리에만 둔다 — 평문 저장 금지. */
export class SafeStorageLlmKeyStore implements LlmKeyStore {
  private readonly file: string;
  private cache: KeyMap | null = null;

  constructor(userDataDir: string) {
    this.file = path.join(userDataDir, LLM_KEY_FILE);
  }

  private get encryptionAvailable(): boolean {
    try {
      return safeStorage.isEncryptionAvailable();
    } catch {
      return false;
    }
  }

  private async read(): Promise<KeyMap> {
    if (this.cache) return this.cache;
    if (!this.encryptionAvailable) return (this.cache = {});
    try {
      const blob = await fs.readFile(this.file);
      const parsed = JSON.parse(safeStorage.decryptString(blob)) as KeyMap;
      this.cache = parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
      this.cache = {}; // 파일이 없거나 다른 계정에서 만든 blob
    }
    return this.cache;
  }

  private async persist(keys: KeyMap): Promise<void> {
    this.cache = keys;
    if (!this.encryptionAvailable) {
      logError(
        'llm-key',
        new Error('safeStorage 사용 불가 — LLM 키를 이번 실행 동안만 유지합니다.'),
      );
      return;
    }
    await writeFileAtomic(this.file, safeStorage.encryptString(JSON.stringify(keys)));
  }

  async status(): Promise<LlmKeyStatus[]> {
    return toStatus(await this.read());
  }

  async set(input: LlmKeyInput): Promise<LlmKeyStatus[]> {
    const apiKey = input.apiKey.trim();
    if (!apiKey) throw new UserFacingError('API 키를 입력해 주세요.');
    if (!LLM_PROVIDERS.includes(input.provider)) {
      throw new UserFacingError('지원하지 않는 LLM 제공자입니다.');
    }
    const keys = { ...(await this.read()), [input.provider]: apiKey };
    await this.persist(keys);
    return toStatus(keys);
  }

  async clear(provider: LlmProvider): Promise<LlmKeyStatus[]> {
    const keys = { ...(await this.read()) };
    delete keys[provider];
    await this.persist(keys);
    if (Object.keys(keys).length === 0) await fs.rm(this.file, { force: true });
    return toStatus(keys);
  }

  async get(provider: LlmProvider): Promise<string | null> {
    return (await this.read())[provider] ?? null;
  }

  /** 파이프라인이 쓰는 유일한 경로 — 원문 키는 메인 프로세스를 벗어나지 않는다. */
  async resolve(): Promise<ResolvedLlmKey | null> {
    return resolveFrom(await this.read());
  }
}

/** 테스트·목 모드용 인메모리 저장소. */
export class MemoryLlmKeyStore implements LlmKeyStore {
  private keys: KeyMap = {};

  async status(): Promise<LlmKeyStatus[]> {
    return toStatus(this.keys);
  }

  async set(input: LlmKeyInput): Promise<LlmKeyStatus[]> {
    this.keys = { ...this.keys, [input.provider]: input.apiKey };
    return toStatus(this.keys);
  }

  async clear(provider: LlmProvider): Promise<LlmKeyStatus[]> {
    const keys = { ...this.keys };
    delete keys[provider];
    this.keys = keys;
    return toStatus(this.keys);
  }

  async get(provider: LlmProvider): Promise<string | null> {
    return this.keys[provider] ?? null;
  }

  async resolve(): Promise<ResolvedLlmKey | null> {
    return resolveFrom(this.keys);
  }
}
