import { useState, useEffect } from 'react';
import { Plug, PlugZap, Wifi } from 'lucide-react';
import { api } from '../utils/api';
import type { SavedCredentials, StatusResponse } from '../types';

interface Props {
  status: StatusResponse | null;
  onRefresh: () => void;
}

export default function ConnectionBar({ status, onRefresh }: Props) {
  const [credentials, setCredentials] = useState<SavedCredentials[]>([]);
  const [loading, setLoading] = useState(false);
  const [autoConnectAttempted, setAutoConnectAttempted] = useState(false);
  const [error, setError] = useState('');

  const connected = status?.connected ?? false;

  useEffect(() => {
    api.getCredentials().then(setCredentials).catch(() => {});
  }, []);

  useEffect(() => {
    if (!autoConnectAttempted && !connected && credentials.length > 0) {
      setAutoConnectAttempted(true);
      handleAutoConnect();
    }
  }, [credentials, connected, autoConnectAttempted]);

  const handleAutoConnect = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.autoConnect();
      if (result.connected) {
        onRefresh();
      } else {
        setError(result.reason || 'Could not connect automatically');
      }
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

  if (connected && status?.account) {
    return (
      <div className="card" style={{ padding: '12px 20px' }}>
        <div className="flex justify-between items-center">
          <div className="flex items-center gap-3">
            <Wifi size={16} className="text-green" />
            <span style={{ fontWeight: 600, fontSize: 14 }}>
              {status.account.name || `Account ${status.account.login}`}
            </span>
            <span className="badge badge-green">DEMO</span>
          </div>
          <button className="btn btn-secondary btn-sm" onClick={handleDisconnect} disabled={loading}>
            <PlugZap size={12} /> Disconnect
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: '12px 20px' }}>
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-3">
          <span className="status-dot disconnected" />
          <span style={{ fontSize: 13 }}>Not connected to trading account</span>
          {error && <span className="text-red text-sm">{error}</span>}
        </div>
        <div className="flex items-center gap-2">
          {credentials.length > 0 ? (
            <button className="btn btn-primary" onClick={handleAutoConnect} disabled={loading} style={{ fontSize: 13 }}>
              {loading ? <span className="loading-spinner" /> : <Plug size={14} />}
              Quick Connect
            </button>
          ) : (
            <a href="#/connection" className="btn btn-primary" style={{ textDecoration: 'none', fontSize: 13 }}>
              <Plug size={14} /> Connect Account
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
