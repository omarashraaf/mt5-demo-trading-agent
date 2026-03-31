import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { Session, User } from '@supabase/supabase-js';
import { supabase, supabaseEnabled } from '@/lib/supabase';
import { api } from '@/utils/api';

type AuthContextValue = {
  loading: boolean;
  supabaseEnabled: boolean;
  user: User | null;
  session: Session | null;
  role: 'admin' | 'user';
  approved: boolean;
  accessStatus: 'pending' | 'approved' | 'rejected' | string;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

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

  const refreshProfile = async () => {
    if (!supabase) return;
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
    if (!supabase) {
      setLoading(false);
      return;
    }

    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session ?? null);
      setRole(resolveRole(data.session ?? null));
      if (data.session) {
        void refreshProfile();
      } else {
        setApproved(false);
        setAccessStatus('pending');
      }
      setLoading(false);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession);
      setRole(resolveRole(nextSession));
      if (nextSession) {
        void refreshProfile();
      } else {
        setApproved(false);
        setAccessStatus('pending');
      }
    });

    return () => subscription.unsubscribe();
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
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw new Error(error.message);
        await refreshProfile();
      },
      signUp: async (email: string, password: string) => {
        if (!supabase) throw new Error('Supabase not configured');
        const { error } = await supabase.auth.signUp({ email, password });
        if (error) throw new Error(error.message);
      },
      signOut: async () => {
        if (!supabase) return;
        const { error } = await supabase.auth.signOut();
        if (error) throw new Error(error.message);
        setRole('user');
        setApproved(false);
        setAccessStatus('pending');
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
