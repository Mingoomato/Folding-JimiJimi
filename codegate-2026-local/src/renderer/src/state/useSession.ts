import { useCallback, useEffect, useState } from 'react';
import type { Session } from '@contracts';

/** L4 — 로그인 상태. 메인이 세션을 바꾸면 푸시로 따라간다. */
export function useSession() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    window.codegate.auth
      .session()
      .then((s) => alive && setSession(s))
      .finally(() => alive && setLoading(false));
    const off = window.codegate.auth.onChanged(setSession);
    return () => {
      alive = false;
      off();
    };
  }, []);

  const login = useCallback(async () => {
    const s = await window.codegate.auth.login({ provider: 'google' });
    setSession(s);
    return s;
  }, []);

  const logout = useCallback(async () => {
    await window.codegate.auth.logout();
    setSession({ authenticated: false });
  }, []);

  return { session, loading, login, logout };
}
