import { useState } from 'react';
import { Check, KeyRound, Trash2 } from 'lucide-react';
import type { LlmKeyStatus, LlmProvider } from '@contracts';
import { LLM_PROVIDER_LABEL } from '@contracts';
import { Button } from '@/ui/Button';
import { Input } from '@/ui/Input';
import { cn } from '@/lib/cn';

const PROVIDERS: LlmProvider[] = ['gemini', 'openai', 'anthropic'];

/**
 * enrichment LLM 키 입력 (스펙 v1.4 §1 L4 — "LLM API 키 설정 화면").
 *
 * 입력한 키는 IPC로 넘긴 즉시 화면에서 지운다. 저장은 메인이 safeStorage 로 한다.
 * 다시 읽어올 수 없으므로 화면에는 끝 4자리만 표시된다.
 */
export function LlmKeyForm({
  keys,
  onSave,
  onClear,
}: {
  keys: LlmKeyStatus[];
  onSave: (provider: LlmProvider, apiKey: string) => Promise<void>;
  onClear: (provider: LlmProvider) => Promise<void>;
}) {
  const [provider, setProvider] = useState<LlmProvider>('anthropic');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const configured = keys.filter((k) => k.configured);

  async function submit() {
    const key = draft.trim();
    if (!key || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(provider, key);
      setDraft(''); // 메모리에 남겨두지 않는다
    } catch (e) {
      setError(e instanceof Error ? e.message : '키를 저장하지 못했습니다.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {configured.length > 0 && (
        <ul className="flex flex-col gap-2">
          {configured.map((k) => (
            <li
              key={k.provider}
              className="flex items-center gap-3 rounded-md border border-ink-100 px-4 py-3"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-green-50 text-green-600">
                <Check size={15} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-semibold text-ink-800">
                  {LLM_PROVIDER_LABEL[k.provider]}
                </div>
                <div className="mt-0.5 font-mono text-2xs text-ink-400">
                  {k.hint ? `…${k.hint}` : '설정됨'}
                </div>
              </div>
              <button
                onClick={() => onClear(k.provider)}
                aria-label={`${LLM_PROVIDER_LABEL[k.provider]} 키 삭제`}
                className="shrink-0 rounded-sm p-1.5 text-ink-300 transition-colors hover:bg-red-50 hover:text-red-500 fold-focus"
              >
                <Trash2 size={15} />
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-col gap-2.5">
        <div className="flex gap-1.5">
          {PROVIDERS.map((p) => (
            <button
              key={p}
              onClick={() => setProvider(p)}
              className={cn(
                'rounded-full px-3 py-1.5 text-xs font-semibold transition-colors duration-[--dur-fast] fold-focus',
                provider === p
                  ? 'bg-blue-50 text-blue-600'
                  : 'text-ink-500 hover:bg-ink-50',
              )}
            >
              {LLM_PROVIDER_LABEL[p]}
            </button>
          ))}
        </div>

        <div className="flex gap-2">
          <Input
            type="password"
            value={draft}
            autoComplete="off"
            spellCheck={false}
            placeholder={`${LLM_PROVIDER_LABEL[provider]} API 키 붙여넣기`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
          <Button onClick={submit} loading={busy} disabled={!draft.trim()} className="shrink-0">
            저장
          </Button>
        </div>

        {error && <p className="text-xs text-red-600">{error}</p>}

        <p className="flex items-start gap-2 text-2xs leading-relaxed text-ink-400">
          <KeyRound size={13} className="mt-0.5 shrink-0" />
          키는 이 컴퓨터에만 암호화 저장됩니다(safeStorage). 문서 분석에 쓰이며, 로그인 계정과는
          별개의 자격증명입니다.
        </p>
      </div>
    </div>
  );
}
