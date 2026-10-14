import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { Session, User } from '@supabase/supabase-js';
import { supabase, supabaseEnabled } from '@/lib/supabase';
import { api } from '@/utils/api';
import { runtimeConfig } from '@/config';

type AuthContextValue = {
  loading: boolean;
  supabaseEnabled: boolean;
  user: User | null;
  session: Session | null;
  role: 'admin' | 'user';
  approved: boolean;
  accessStatus: 'pending' | 'approved' | 'rejected' | string;
  signIn: (email: string, password: string) => Promise<{
    role: 'admin' | 'user';
    approved: boolean;
    accessStatus: 'pending' | 'approved' | 'rejected' | string;
  }>;
  signUp: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);
const AUTH_BOOTSTRAP_TIMEOUT_MS = 6000;
const AUTH_ACTION_TIMEOUT_MS = 10000;

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function normalizeAuthError(error: unknown, fallback: string): Error {
  const raw = String((error as any)?.message || error || '').trim();
  const lower = raw.toLowerCase();
  if (
    lower.includes('failed to fetch')
    || lower.includes('network request failed')
    || lower.includes('load failed')
    || lower.includes('timed out')
    || lower.includes('timeout')
  ) {
    return new Error('Auth service is unreachable from this machine. Use local runtime mode, or check the Supabase URL/network before logging in.');
  }
  return new Error(raw || fallback);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [session, setSession] = useState<Session | null>(null);
  const [role, setRole] = useState<'admin' | 'user'>('user');
  const [approved, setApproved] = useState(false);
  const [accessStatus, setAccessStatus] = useState<'pending' | 'approved' | 'rejected' | string>('pending');

  const resolveRole = (nextSession: Session | null): 'admin' | 'user' => {
    const appMetaRole = String(nextSession?.user?.app_metadata?.role || '').toLowerCase();
    return appMetaRole === 'admin' ? 'admin' : 'user';
  };

  const resolveApprovalFromSession = (nextSession: Session | null) => {
    const status = String(nextSession?.user?.user_metadata?.access_status || 'pending').toLowerCase();
    const nextApproved = status === 'approved' || resolveRole(nextSession) === 'admin';
    return { status, approved: nextApproved };
  };

  const refreshProfile = async () => {
    if (!supabase) return;
    try {
      const { data, error } = await withTimeout(
        supabase.auth.getUser(),
        AUTH_BOOTSTRAP_TIMEOUT_MS,
        'Timed out while loading the auth profile.',
      );
      if (!error && data?.user) {
        const currentSession = await withTimeout(
          supabase.auth.getSession(),
          AUTH_BOOTSTRAP_TIMEOUT_MS,
          'Timed out while refreshing the auth session.',
        );
        const nextSession = currentSession.data.session ?? session;
        const status = String(data.user.user_metadata?.access_status || 'pending').toLowerCase();
        const nextRole = resolveRole(nextSession);
        setRole(nextRole);
        setApproved(status === 'approved' || nextRole === 'admin');
        setAccessStatus(status);
        return;
      }
    } catch {
      // fallback to backend me endpoint below
    }

    try {
      const me = await api.authMe();
      const nextRole = String(me.role || '').toLowerCase() === 'admin' ? 'admin' : 'user';
      setRole(nextRole);
      setApproved(Boolean(me.approved));
      setAccessStatus(String(me.access_status || 'pending'));
    } catch {
      setRole(resolveRole(session));
      setApproved(false);
      setAccessStatus('pending');
    }
  };

  useEffect(() => {
    if (!runtimeConfig.requireAuth) {
      setLoading(false);
      return;
    }

    if (!supabase) {
      setLoading(false);
      return;
    }

    let cancelled = false;

    const initializeSession = async () => {
      try {
        const { data } = await withTimeout(
          supabase.auth.getSession(),
          AUTH_BOOTSTRAP_TIMEOUT_MS,
          'Timed out while checking the saved auth session.',
        );
        if (cancelled) return;
        const nextSession = data.session ?? null;
        setSession(nextSession);
        setRole(resolveRole(nextSession));
        if (nextSession) {
          const { status, approved } = resolveApprovalFromSession(nextSession);
          setApproved(approved);
          setAccessStatus(status);
          void refreshProfile();
        } else {
          setApproved(false);
          setAccessStatus('pending');
        }
      } catch (error) {
        if (cancelled) return;
        console.warn('Auth session check failed; showing login screen.', error);
        setSession(null);
        setRole('user');
        setApproved(false);
        setAccessStatus('pending');
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void initializeSession();

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession);
      setRole(resolveRole(nextSession));
      if (nextSession) {
        const { status, approved } = resolveApprovalFromSession(nextSession);
        setApproved(approved);
        setAccessStatus(status);
        void refreshProfile();
      } else {
        setApproved(false);
        setAccessStatus('pending');
      }
    });

    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
      supabaseEnabled,
      user: session?.user ?? null,
      session,
      role,
      approved,
      accessStatus,
      signIn: async (email: string, password: string) => {
        if (!supabase) throw new Error('Supabase not configured');
        let data;
        try {
          const response = await withTimeout(
            supabase.auth.signInWithPassword({ email, password }),
            AUTH_ACTION_TIMEOUT_MS,
            'Timed out while signing in.',
          );
          if (response.error) throw response.error;
          data = response.data;
        } catch (error) {
          throw normalizeAuthError(error, 'Sign in failed.');
        }
        const nextSession = data.session ?? null;
        let nextRole = resolveRole(nextSession);
        let status = String(data.user?.user_metadata?.access_status || 'pending').toLowerCase();
        let nextApproved = status === 'approved' || nextRole === 'admin';
        try {
          const me = await api.authMe();
          nextRole = String(me.role || '').toLowerCase() === 'admin' ? 'admin' : 'user';
          status = String(me.access_status || status || 'pending').toLowerCase();
          nextApproved = Boolean(me.approved) || nextRole === 'admin';
        } catch {
          // Supabase metadata is the fallback when the backend profile is unavailable.
        }
        setSession(nextSession);
        setRole(nextRole);
        setApproved(nextApproved);
        setAccessStatus(status);
        return {
          role: nextRole,
          approved: nextApproved,
          accessStatus: status,
        };
      },
      signUp: async (email: string, password: string) => {
        if (!supabase) throw new Error('Supabase not configured');
        try {
          const { error } = await withTimeout(
            supabase.auth.signUp({
              email,
              password,
              options: {
                data: {
                  access_status: 'pending',
                },
              },
            }),
            AUTH_ACTION_TIMEOUT_MS,
            'Timed out while creating the account.',
          );
          if (error) throw error;
        } catch (error) {
          throw normalizeAuthError(error, 'Sign up failed.');
        }
      },
      signOut: async () => {
        if (!supabase) return;
        try {
          const { error } = await withTimeout(
            supabase.auth.signOut({ scope: 'local' }),
            AUTH_BOOTSTRAP_TIMEOUT_MS,
            'Timed out while signing out locally.',
          );
          if (error) throw new Error(error.message);
        } catch (error) {
          console.warn('Local sign out did not complete cleanly; clearing UI session state.', error);
        } finally {
          setSession(null);
          setRole('user');
          setApproved(false);
          setAccessStatus('pending');
        }
      },
      refreshProfile,
    }),
    [accessStatus, approved, loading, role, session],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
