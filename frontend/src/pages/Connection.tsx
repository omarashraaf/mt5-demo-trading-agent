import { useState, useEffect } from 'react';
import { Plug, PlugZap, Search, Trash2, AlertTriangle, HelpCircle, CheckCircle, ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '../utils/api';
import type { StatusResponse, SavedCredentials } from '../types';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
}

export default function Connection({ status, onRefresh }: Props) {
  const [account, setAccount] = useState('');
  const [password, setPassword] = useState('');
  const [server, setServer] = useState('');
  const [terminalPath, setTerminalPath] = useState('');
  const [rememberCredentials, setRememberCredentials] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [verifyResult, setVerifyResult] = useState<any>(null);
  const [savedCreds, setSavedCreds] = useState<SavedCredentials[]>([]);

  const connected = status?.connected ?? false;

  useEffect(() => {
    api.getCredentials().then((creds) => {
      setSavedCreds(creds);
      if (creds.length > 0 && !account) {
        const c = creds[0];
        setAccount(String(c.account));
        setServer(c.server);
        setTerminalPath(c.terminal_path || '');
      }
    }).catch(() => {});
  }, []);

  const handleDeleteCredentials = async (acct: number) => {
    try {
      await api.deleteCredentials(acct);
      setSavedCreds((prev) => prev.filter((c) => c.account !== acct));
    } catch {}
  };

  const handleConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.connect({
        account: parseInt(account),
        password,
        server,
        terminal_path: terminalPath || undefined,
        save_credentials: rememberCredentials,
      });
      if (rememberCredentials && result?.credential_status?.saved === false) {
        setError(result.credential_status.reason || 'Connected, but credentials could not be saved securely.');
      }
      onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await api.disconnect();
      onRefresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async () => {
    setLoading(true);
    setVerifyResult(null);
    try {
      const result = await api.verifyTerminal(terminalPath || undefined);
      setVerifyResult(result);
    } catch (e: any) {
      setVerifyResult({ ok: false, error: e.message });
    } finally {
      setLoading(false);
    }
  };

  const handleAutoConnect = async (acct?: number) => {
    setLoading(true);
    setError('');
    try {
      const result = await api.autoConnect(acct);
      if (!result.connected) {
        setError(result.reason || 'Auto-connect failed');
      } else {
        onRefresh();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 700, margin: '0 auto' }}>
      <div className="page-header">
        <h2>Connect to MetaTrader 5</h2>
        <p>Enter your demo account details to get started</p>
      </div>

      {/* Help box for beginners */}
      {!connected && (
        <div style={{
          background: 'rgba(59, 130, 246, 0.06)',
          border: '1px solid rgba(59, 130, 246, 0.15)',
          borderRadius: 8,
          padding: '16px 20px',
          marginBottom: 20,
          fontSize: 13,
          color: 'var(--text-secondary)',
          lineHeight: 1.6,
        }}>
          <div className="flex items-center gap-2" style={{ fontWeight: 600, marginBottom: 8, color: 'var(--accent-blue)' }}>
            <HelpCircle size={14} />
            Where do I find these details?
          </div>
          <ol style={{ paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <li>Open your <strong>MetaTrader 5</strong> application on your computer</li>
            <li>Your <strong>Account Number</strong> is shown in the Navigator panel (left side) or the title bar</li>
            <li>Your <strong>Password</strong> and <strong>Server</strong> were emailed to you when you created the demo account</li>
            <li>If you don't have a demo account yet, open MT5 and select <strong>File &gt; Open an Account</strong></li>
          </ol>
        </div>
      )}

      {/* Connected state */}
      {connected && status?.account && (
        <div className="card" style={{ marginBottom: 20 }}>
          <div className="flex items-center gap-3" style={{ marginBottom: 16 }}>
            <CheckCircle size={24} style={{ color: 'var(--accent-green)' }} />
            <div>
              <div style={{ fontWeight: 600, fontSize: 16 }}>Connected Successfully!</div>
              <div className="text-muted" style={{ fontSize: 13 }}>Your MT5 demo account is ready</div>
            </div>
          </div>
          <div style={{
            background: 'var(--bg-tertiary)',
            borderRadius: 8,
            padding: 16,
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 12,
            fontSize: 13,
            marginBottom: 16,
          }}>
            <div>
              <span className="text-muted">Account: </span>
              <span style={{ fontWeight: 600 }}>{status.account.name || status.account.login}</span>
            </div>
            <div>
              <span className="text-muted">Server: </span>
              <span>{status.account.server}</span>
            </div>
            <div>
              <span className="text-muted">Balance: </span>
              <span style={{ fontWeight: 600 }}>{status.account.balance.toFixed(2)} {status.account.currency}</span>
            </div>
            <div>
              <span className="text-muted">Type: </span>
              <span className="badge badge-green">DEMO</span>
            </div>
          </div>
          <div className="flex gap-2">
            <a href="#/" className="btn btn-primary" style={{ textDecoration: 'none' }}>
              Go to Dashboard
            </a>
            <button className="btn btn-secondary" onClick={handleDisconnect} disabled={loading}>
              <PlugZap size={14} /> Disconnect
            </button>
          </div>
        </div>
      )}

      {/* Login form */}
      {!connected && (
        <div className="card">
          <h3 style={{ fontSize: 15, fontWeight: 600, marginBottom: 20 }}>Account Details</h3>

          {error && (
            <div className="error-banner" style={{ fontSize: 13 }}>
              {error}
            </div>
          )}

          <div className="form-group">
            <label>Account Number</label>
            <input
              className="form-input"
              type="text"
              placeholder="e.g. 12345678"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              style={{ fontSize: 15, padding: '12px 16px' }}
            />
          </div>

          <div className="form-group">
            <label>Password</label>
            <input
              className="form-input"
              type="password"
              placeholder="Your MT5 password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={{ fontSize: 15, padding: '12px 16px' }}
            />
          </div>

          <div className="form-group">
            <label>Server</label>
            <input
              className="form-input"
              type="text"
              placeholder="e.g. MetaQuotes-Demo"
              value={server}
              onChange={(e) => setServer(e.target.value)}
              style={{ fontSize: 15, padding: '12px 16px' }}
            />
          </div>

          <label className="flex items-center gap-2" style={{ fontSize: 13, marginBottom: 16 }}>
            <input
              type="checkbox"
              checked={rememberCredentials}
              onChange={(e) => setRememberCredentials(e.target.checked)}
              disabled={status?.credential_storage_available === false}
            />
            Remember this account on this device
          </label>
          {status?.credential_storage_available === false && (
            <div className="warning-banner" style={{ fontSize: 11, padding: '8px 12px', marginBottom: 12 }}>
              <AlertTriangle size={12} />
              Secure credential storage is unavailable, so saved login is disabled.
            </div>
          )}

          {/* Advanced settings - hidden by default */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--text-muted)',
              fontSize: 12,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              padding: '8px 0',
              marginBottom: showAdvanced ? 8 : 0,
            }}
          >
            {showAdvanced ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            Advanced settings
          </button>

          {showAdvanced && (
            <>
              <div className="form-group">
                <label>Terminal Path (usually auto-detected)</label>
                <input
                  className="form-input"
                  type="text"
                  placeholder="Leave empty for auto-detect"
                  value={terminalPath}
                  onChange={(e) => setTerminalPath(e.target.value)}
                />
              </div>
              <button className="btn btn-secondary btn-sm mb-4" onClick={handleVerify} disabled={loading}>
                <Search size={12} /> Verify Terminal
              </button>
              {verifyResult && (
                <div className={`text-sm ${verifyResult.ok ? 'text-green' : 'text-red'}`} style={{ marginBottom: 12 }}>
                  {verifyResult.ok
                    ? `Found: ${verifyResult.name} (Build ${verifyResult.build})`
                    : `Not found: ${verifyResult.error}`
                  }
                </div>
              )}
            </>
          )}

          <button
            className="btn btn-primary"
            onClick={handleConnect}
            disabled={loading || !account || !password || !server}
            style={{ fontSize: 15, padding: '14px 32px', width: '100%', justifyContent: 'center', marginTop: 8 }}
          >
            {loading ? <span className="loading-spinner" /> : <Plug size={16} />}
            Connect to MT5
          </button>
        </div>
      )}

      {/* Saved credentials */}
      {savedCreds.length > 0 && !connected && (
        <div className="card mt-4">
          <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>Saved Accounts</h3>
          <div className="warning-banner" style={{ fontSize: 11, padding: '8px 12px' }}>
            <CheckCircle size={12} />
            Passwords are stored outside SQLite and are never returned to the UI.
          </div>
          {savedCreds.map((c) => (
            <div key={c.account} className="flex justify-between items-center" style={{
              padding: '10px 0',
              borderBottom: '1px solid var(--border)',
              fontSize: 13,
            }}>
              <div>
                <span style={{ fontWeight: 600 }}>{c.account}</span>
                <span className="text-muted"> on {c.server}</span>
              </div>
              <div className="flex gap-2">
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => {
                    setAccount(String(c.account));
                    setServer(c.server);
                    setTerminalPath(c.terminal_path || '');
                  }}
                >
                  Load
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleAutoConnect(c.account)}>
                  Auto Connect
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => handleDeleteCredentials(c.account)}>
                  <Trash2 size={10} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
