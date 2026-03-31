import { useState } from 'react';
import { useAuth } from '@/context/AuthContext';
import { api } from '@/utils/api';

export default function AuthPage() {
  const { signIn, signOut, supabaseEnabled } = useAuth();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      if (mode === 'login') {
        await signIn(email.trim(), password);
        try {
          const me = await api.authMe();
          if (!me.approved) {
            await signOut();
            throw new Error(
              me.access_status === 'rejected'
                ? 'Your account request was rejected by admin.'
                : 'Your account is pending admin approval.',
            );
          }
        } catch (err) {
          if (err instanceof Error) {
            throw err;
          }
          throw new Error('Could not verify account approval status.');
        }
      } else {
        const res = await api.publicRegister({ email: email.trim(), password });
        setMessage(res.message || 'Registration submitted. Wait for admin approval.');
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'Authentication failed';
      setError(detail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', width: '100%', display: 'grid', placeItems: 'center', padding: 20 }}>
      <div className="card" style={{ width: '100%', maxWidth: 460, marginBottom: 0 }}>
        <div className="page-header" style={{ marginBottom: 14 }}>
          <h2>LinkTrade</h2>
          <p>Sign in to open your trading workspace.</p>
        </div>
        {!supabaseEnabled && (
          <div className="error-banner">
            Supabase is not configured. Set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` in frontend `.env`.
          </div>
        )}
        {error && <div className="error-banner">{error}</div>}
        {message && <div className="warning-banner">{message}</div>}
        <form onSubmit={submit}>
          <div className="form-group">
            <label>Email</label>
            <input
              className="form-input"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              disabled={busy || !supabaseEnabled}
            />
          </div>
          <div className="form-group">
            <label>Password</label>
            <input
              className="form-input"
              type="password"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={mode === 'register' ? 6 : 1}
              disabled={busy || !supabaseEnabled}
            />
          </div>
          <div className="flex gap-2">
            <button className="btn btn-primary" type="submit" disabled={busy || !supabaseEnabled}>
              {busy ? 'Please wait...' : mode === 'login' ? 'Login' : 'Register'}
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              onClick={() => {
                setMode((prev) => (prev === 'login' ? 'register' : 'login'));
                setError(null);
                setMessage(null);
              }}
              disabled={busy}
            >
              {mode === 'login' ? 'Create account' : 'Back to login'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
