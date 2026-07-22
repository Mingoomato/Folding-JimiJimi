import { useCallback, useEffect, useState } from 'react';
import type { LlmKeyStatus, LlmProvider } from '@contracts';

/**
 * L4 — 문서 분석 LLM 키 상태.
 *
 * 원문 키는 메인 프로세스만 안다. 여기서 다루는 건 "설정됨 여부 + 끝 4자리"뿐이다.
 * 구독 모델에서는 앱이 키를 직접 공급하므로(`managed`) 화면에 입력폼을 띄우지 않는다.
 */
export function useLlmKey() {
  const [keys, setKeys] = useState<LlmKeyStatus[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    window.codegate.llmKey
      .status()
      .then((k) => alive && setKeys(k))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const save = useCallback(async (provider: LlmProvider, apiKey: string) => {
    setKeys(await window.codegate.llmKey.set({ provider, apiKey }));
  }, []);

  const clear = useCallback(async (provider: LlmProvider) => {
    setKeys(await window.codegate.llmKey.clear(provider));
  }, []);

  return {
    keys,
    loading,
    /** 하나라도 설정돼 있으면 빌드 가능 */
    configured: keys.some((k) => k.configured),
    /** 앱이 구독의 일부로 키를 공급 중 — 입력폼 대신 안내를 보여준다 */
    managed: keys.some((k) => k.managed),
    save,
    clear,
  };
}
