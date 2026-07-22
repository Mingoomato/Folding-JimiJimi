import { useState } from 'react';
import { useSession } from './state/useSession';
import { useWorkspace } from './state/useWorkspace';
import { useApproval } from './state/useApproval';
import { LoginScreen } from './screens/LoginScreen';
import { OnboardingScreen } from './screens/OnboardingScreen';
import { MainScreen } from './screens/MainScreen';
import { SettingsScreen } from './screens/SettingsScreen';
import { ApprovalModal } from './components/ApprovalModal';
import { Mark } from './ui/Wordmark';

/**
 * 화면 전이 (스펙 v1.3 §3):
 *   로그인 → (등록 폴더 없음) 온보딩 → 메인 3분할
 * 승인 모달과 설정은 어느 화면 위에서든 뜰 수 있게 최상위에 둔다.
 */
export function App() {
  const { session, loading: sessionLoading, login, logout } = useSession();
  const workspace = useWorkspace();
  const approval = useApproval();
  const [settingsOpen, setSettingsOpen] = useState(false);

  const booting = sessionLoading || workspace.loading;

  return (
    <>
      {booting ? (
        <BootScreen />
      ) : !session?.authenticated ? (
        <LoginScreen onLogin={login} />
      ) : workspace.roots.length === 0 ? (
        <OnboardingScreen workspace={workspace} session={session} />
      ) : (
        <MainScreen
          workspace={workspace}
          session={session}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      )}

      {/* 승인 게이트 — 메인이 응답을 기다리며 멈춰 있으므로 항상 마운트되어 있어야 한다 */}
      <ApprovalModal
        pending={approval.pending}
        onApprove={approval.approve}
        onReject={approval.reject}
      />

      {session?.authenticated && (
        <SettingsScreen
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          session={session}
          workspace={workspace}
          onLogout={logout}
        />
      )}
    </>
  );
}

function BootScreen() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 bg-ink-50">
      <Mark size={40} />
      <span className="text-sm text-ink-400">불러오는 중…</span>
    </div>
  );
}
