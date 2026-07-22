import { useState } from 'react';
import { FileText, Quote, ShieldCheck } from 'lucide-react';
import type { Session } from '@contracts';
import { Button } from '@/ui/Button';
import { Logo, MarkWithText } from '@/ui/Wordmark';

/**
 * L4 로그인.
 * Folding 앱 킷의 split-screen 온보딩 구성을 따른다 —
 * 좌: ink 그라디언트 레일(라디얼 블루 글로우), 우: 폼.
 */
export function LoginScreen({ onLogin }: { onLogin: () => Promise<Session> }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const webDemo = document.documentElement.dataset.runtime === 'web-demo';

  async function login() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onLogin();
    } catch (err) {
      // 어떤 실패든 사용자 문장으로 (스펙 v1.3 §5 — silent fail 금지)
      setError(err instanceof Error ? err.message : '로그인에 실패했습니다. 다시 시도해 주세요.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid h-full grid-cols-[1.05fr_1fr] overflow-hidden max-[980px]:grid-cols-1">
      {/* ── 좌: 브랜드 레일 ── */}
      <aside
        className="relative flex flex-col justify-between overflow-hidden p-12 max-[980px]:hidden"
        style={{ background: 'var(--gradient-ink)' }}
        data-drag-region
      >
        {/*
          흐르는 그라디언트 — 배경을 화면보다 넓게(220%) 깔고 위치만 천천히 움직인다.
          `background-position` 만 바뀌므로 레이아웃·페인트를 다시 하지 않는다(합성만).
          로그인 화면은 오래 떠 있을 수 있어 18초로 느리게 돈다.
        */}
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage:
              'linear-gradient(115deg, rgba(43,93,253,0) 12%, rgba(43,93,253,.42) 38%, rgba(92,134,254,.16) 58%, rgba(43,93,253,0) 86%)',
            backgroundSize: '220% 220%',
            animation: 'fold-gradient-drift 18s ease-in-out infinite',
          }}
        />

        {/* 라디얼 블루 글로우 — 은은하게, 강하지 않게 */}
        <div
          className="pointer-events-none absolute -right-24 -top-24 h-[420px] w-[420px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(43,93,253,.55) 0%, transparent 68%)' }}
        />
        <div
          className="pointer-events-none absolute -bottom-32 -left-20 h-[380px] w-[380px] rounded-full"
          style={{ background: 'radial-gradient(circle, rgba(92,134,254,.28) 0%, transparent 70%)' }}
        />

        {/* 어두운 면이라 네이비 워드마크 대신 마크 + 흰 텍스트 조판을 쓴다 */}
        <MarkWithText className="relative" markSize={30} fontSize={24} />

        <div className="relative flex flex-col gap-5">
          <span className="fold-eyebrow text-blue-300">LOCAL DOCUMENT WIKI</span>
          <h1 className="text-h1 font-extrabold leading-tight tracking-tighter text-white">
            문서는 그대로,
            <br />
            답은 근거와 함께.
          </h1>
          <p className="max-w-[460px] text-base leading-relaxed text-ink-300">
            등록한 폴더의 문서를 Folding이 읽어 위키로 정리합니다. 질문하면 원문을 짚어 답하고,
            수정은 승인을 거쳐서만 반영됩니다.
          </p>
        </div>

        <ul className="relative flex flex-col gap-3.5">
          <RailPoint icon={<ShieldCheck size={17} />} text="위키는 이 컴퓨터에만 저장됩니다" />
          <RailPoint icon={<Quote size={17} />} text="모든 답변에 문서·리비전·섹션 인용" />
          <RailPoint icon={<FileText size={17} />} text="원본은 백업 후 원자적으로만 수정" />
        </ul>
      </aside>

      {/* ── 우: 폼 ── */}
      <main className="flex items-center justify-center bg-white px-8" data-drag-region>
        <div className="w-full max-w-[380px]" data-drag-region={undefined}>
          <div className="mb-8 hidden max-[980px]:block">
            <Logo height={26} />
          </div>

          <h2 className="text-h3 font-bold tracking-tight text-ink-950">로그인</h2>
          <p className="mt-2 text-sm leading-relaxed text-ink-500">
            {webDemo
              ? 'Railway 웹 데모를 시작합니다. 실제 계정이나 API 키는 입력하지 않습니다.'
              : '시스템 브라우저에서 Google 계정으로 인증합니다. 비밀번호는 Folding 앱이나 백엔드로 전달되지 않습니다.'}
          </p>

          {error && (
            <div
              role="alert"
              className="mt-4 rounded-md border border-red-100 bg-red-50 px-3.5 py-3 text-xs text-red-600"
            >
              {error}
            </div>
          )}

          {/*
            구글 브랜드 가이드: 컬러 "G" 는 흰 배경 위에 둔다.
            사용자에게도 이 형태가 가장 익숙해 인지 속도가 빠르다.
          */}
          <Button
            type="button"
            variant="secondary"
            size="lg"
            fullWidth
            loading={busy}
            className="mt-7"
            iconLeft={<GoogleGlyph />}
            onClick={() => void login()}
          >
            {busy ? '준비하는 중…' : webDemo ? '웹 데모 시작' : 'Google로 로그인'}
          </Button>

          <p className="mt-5 text-center text-xs leading-relaxed text-ink-400">
            {webDemo ? (
              <>
                선택한 폴더의 파일명은 브라우저 메모리에서만 사용됩니다.
                <br />
                실제 문서 분석과 원본 작업은 데스크톱 앱에서만 수행합니다.
              </>
            ) : (
              <>
                로그인 창은 기본 브라우저에서 열립니다.
                <br />
                이미 만들어진 위키는 오프라인에서도 검색·질의할 수 있어요.
              </>
            )}
          </p>
        </div>
      </main>
    </div>
  );
}

function RailPoint({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <li className="flex items-center gap-3 text-sm text-ink-200">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-white/10 text-blue-300">
        {icon}
      </span>
      {text}
    </li>
  );
}

/**
 * 구글 "G" 마크.
 * 브랜드 색을 그대로 쓴다 — 단색으로 바꾸거나 색을 임의로 조정하지 않는다.
 */
function GoogleGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z"
      />
      <path
        fill="#34A853"
        d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7C7.96 41.07 15.4 46 24 46z"
      />
      <path
        fill="#FBBC05"
        d="M11.69 28.18c-.44-1.32-.69-2.73-.69-4.18s.25-2.86.69-4.18v-5.7H4.34A21.99 21.99 0 0 0 2 24c0 3.55.85 6.91 2.34 9.88l7.35-5.7z"
      />
      <path
        fill="#EA4335"
        d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z"
      />
    </svg>
  );
}
