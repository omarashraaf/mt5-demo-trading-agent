import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, NavLink, useLocation } from 'react-router-dom';
import { api } from '@/utils/api';
import type { ApiKeysHealthResponse } from '@/types';

type AccessItem = {
  id: number;
  user_id: string;
  email: string;
  status: string;
  requested_at: number;
  approved_at?: number | null;
};

type ActivityItem = {
  id: number;
  timestamp: number;
  user_email: string;
  role: string;
  action: string;
  path: string;
  method: string;
  status_code: number;
};

type ApiHealthItem = ApiKeysHealthResponse['items'][number];

function normalizeAdminError(err: unknown): string {
  if (err instanceof Error) {
    const msg = String(err.message || '').trim();
    if (!msg) return 'Request failed. Please try again.';
    return msg;
  }
  return 'Request failed. Please try again.';
}

function AdminLogin({ onLoggedIn }: { onLoggedIn: () => void }) {
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await api.adminLogin(username, password);
      localStorage.setItem('linktrade_admin_token', res.token);
      onLoggedIn();
      navigate('/admin/registered', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Admin login failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: '100vh', width: '100%', display: 'grid', placeItems: 'center', padding: 20 }}>
      <div className="card" style={{ width: '100%', maxWidth: 460, marginBottom: 0 }}>
        <div className="page-header">
          <h2>Admin Panel</h2>
          <p>Username + password login</p>
        </div>
        {error && <div className="error-banner">{error}</div>}
        <form onSubmit={submit}>
          <div className="form-group">
            <label>Username</label>
            <input className="form-input" value={username} onChange={(e) => setUsername(e.target.value)} />
          </div>
          <div className="form-group">
            <label>Password</label>
            <input className="form-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? 'Checking...' : 'Login'}
          </button>
        </form>
      </div>
    </div>
  );
}

function AdminUsersTable({
  items,
  actionLabel,
  onAction,
  actionBusyUserId,
}: {
  items: AccessItem[];
  actionLabel: string;
  onAction?: (userId: string) => Promise<void>;
  actionBusyUserId?: string | null;
}) {
  const navigate = useNavigate();
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Email</th>
          <th>Status</th>
          <th>Requested</th>
          <th>Approved</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.id}>
            <td>{item.email}</td>
            <td>{item.status}</td>
            <td>{new Date(item.requested_at * 1000).toLocaleString()}</td>
            <td>{item.approved_at ? new Date(item.approved_at * 1000).toLocaleString() : '-'}</td>
            <td>
              <div className="flex gap-2">
                <button
                  className="btn btn-secondary btn-sm"
                  type="button"
                  onClick={() => navigate(`/admin/user/${item.user_id}`)}
                >
                  Open
                </button>
                {onAction && (
                  <button
                    className="btn btn-primary btn-sm"
                    type="button"
                    disabled={actionBusyUserId === item.user_id}
                    onClick={() => void onAction(item.user_id)}
                  >
                    {actionLabel}
                  </button>
                )}
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RegisteredTab() {
  const [items, setItems] = useState<AccessItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusyUserId, setActionBusyUserId] = useState<string | null>(null);
  const load = async () => {
    setLoading(true);
    try {
      setError(null);
      setInfo(null);
      const res = await api.adminListAccessRequests('pending');
      setItems(res.items as AccessItem[]);
      setInfo('Pending list refreshed.');
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to load registered users');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      {info && <div className="success-banner">{info}</div>}
      <div className="card-header">
        <h3>Registered (Pending Approval)</h3>
        <button className="btn btn-secondary btn-sm" type="button" disabled={loading} onClick={() => void load()}>
          {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>
      <AdminUsersTable
        items={items}
        actionLabel="Approve"
        actionBusyUserId={actionBusyUserId}
        onAction={async (userId) => {
          setInfo(null);
          setError(null);
          setActionBusyUserId(userId);
          try {
            await api.adminUpdateAccessRequest({ user_id: userId, status: 'approved' });
            setInfo('User approved successfully.');
            await load();
          } catch (err) {
            setError(normalizeAdminError(err) || 'Approve failed');
          } finally {
            setActionBusyUserId(null);
          }
        }}
      />
    </div>
  );
}

function CustomersTab() {
  const [items, setItems] = useState<AccessItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    try {
      setError(null);
      setInfo(null);
      const res = await api.adminCustomers();
      setItems(res.items as AccessItem[]);
      setInfo('Customers list refreshed.');
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to load customers');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      {info && <div className="success-banner">{info}</div>}
      <div className="card-header">
        <h3>Customers (Approved)</h3>
        <button className="btn btn-secondary btn-sm" type="button" disabled={loading} onClick={() => void load()}>
          {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>
      <AdminUsersTable items={items} actionLabel="" />
    </div>
  );
}

function ActivityTab() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    try {
      setError(null);
      setInfo(null);
      const res = await api.adminActivity(200);
      setItems(res.activity as ActivityItem[]);
      setInfo('Activity list refreshed.');
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to load users activity');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      {info && <div className="success-banner">{info}</div>}
      <div className="card-header">
        <h3>Users Activity</h3>
        <button className="btn btn-secondary btn-sm" type="button" disabled={loading} onClick={() => void load()}>
          {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>Time</th>
            <th>User</th>
            <th>Action</th>
            <th>Path</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>{new Date(item.timestamp * 1000).toLocaleString()}</td>
              <td>{item.user_email || '-'}</td>
              <td>{item.action}</td>
              <td>{item.method} {item.path}</td>
              <td>{item.status_code || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ApiKeysHealthTab() {
  const [payload, setPayload] = useState<ApiKeysHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [savingAlerts, setSavingAlerts] = useState(false);

  const load = async (forceRefresh: boolean = false) => {
    setLoading(true);
    try {
      setError(null);
      const res = await api.adminApiKeysHealth(forceRefresh);
      setPayload(res);
      setInfo(`Auto-refreshed at ${new Date().toLocaleTimeString()}`);
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to load API keys health');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(true);
    const timer = setInterval(() => {
      void load(false);
    }, 12000);
    return () => clearInterval(timer);
  }, []);

  const rows = payload?.items || [];
  const overall = payload?.overall_status || 'unknown';
  const emailAlertsEnabled = Boolean(payload?.email_alerts?.enabled);

  const toggleEmailAlerts = async () => {
    const nextEnabled = !emailAlertsEnabled;
    setSavingAlerts(true);
    try {
      setError(null);
      const res = await api.adminSetApiHealthEmailAlerts(nextEnabled);
      setPayload(res);
      setInfo(`Status emails ${nextEnabled ? 'enabled' : 'disabled'}.`);
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to update status email setting');
    } finally {
      setSavingAlerts(false);
    }
  };

  const statusColor = (item: ApiHealthItem) => {
    const st = String(item.status || '').toLowerCase();
    if (st === 'healthy') return 'var(--accent-green)';
    if (st === 'low_credits') return 'var(--accent-yellow)';
    if (st === 'degraded') return 'var(--accent-orange)';
    return 'var(--accent-red)';
  };

  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      {info && <div className="success-banner">{info}</div>}
      <div className="card-header">
        <h3>API Keys Health</h3>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={savingAlerts}
            onClick={() => void toggleEmailAlerts()}
          >
            {savingAlerts ? 'Saving...' : emailAlertsEnabled ? 'Disable status emails' : 'Enable status emails'}
          </button>
          <button className="btn btn-secondary btn-sm" type="button" disabled={loading} onClick={() => void load(true)}>
            {loading ? 'Refreshing...' : 'Refresh now'}
          </button>
        </div>
      </div>
      <div className="grid-3" style={{ marginBottom: 12 }}>
        <div>
          <div className="stat-label">Overall</div>
          <div className="stat-value" style={{ color: overall === 'healthy' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {overall}
          </div>
        </div>
        <div>
          <div className="stat-label">Issues</div>
          <div className="stat-value">{payload?.unhealthy_count ?? 0}</div>
        </div>
        <div>
          <div className="stat-label">Email Alerts</div>
          <div className="stat-value">
            {payload?.email_alerts?.enabled ? (payload?.email_alerts?.configured ? 'Enabled' : 'Enabled (config missing)') : 'Disabled'}
          </div>
        </div>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>API</th>
            <th>Status</th>
            <th>State</th>
            <th>Configured</th>
            <th>Available</th>
            <th>Credits</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr key={item.id}>
              <td>{item.name}</td>
              <td style={{ color: statusColor(item), fontWeight: 700 }}>{item.status}</td>
              <td>{item.state || '-'}</td>
              <td>{item.configured ? 'Yes' : 'No'}</td>
              <td>{item.available ? 'Yes' : 'No'}</td>
              <td>{typeof item.credits_pct === 'number' ? `${item.credits_pct}%` : '-'}</td>
              <td style={{ maxWidth: 420, whiteSpace: 'normal' }}>{item.reason || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {payload?.email_alerts?.last_alert_at ? (
        <div className="text-muted text-sm" style={{ marginTop: 10 }}>
          Last alert sent: {new Date(payload.email_alerts.last_alert_at * 1000).toLocaleString()}
        </div>
      ) : null}
    </div>
  );
}
function UserDetail() {
  const { userId } = useParams();
  const [payload, setPayload] = useState<{ user?: AccessItem; activity?: ActivityItem[] }>({});
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!userId) return;
    setLoading(true);
    setError(null);
    setInfo(null);
    try {
      const res = await api.adminUserDetail(userId);
      setPayload({ user: res.user as AccessItem, activity: res.activity as ActivityItem[] });
      setInfo('User detail refreshed.');
    } catch (err) {
      setError(normalizeAdminError(err) || 'Failed to load user detail');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [userId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!payload.user) return <div className="card">Loading user details...</div>;

  return (
    <div className="card">
      {info && <div className="success-banner">{info}</div>}
      <div className="card-header">
        <h3>User Detail</h3>
        <button className="btn btn-secondary btn-sm" type="button" disabled={loading} onClick={() => void load()}>
          {loading ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>
      <div className="grid-3">
        <div>
          <div className="stat-label">Email</div>
          <div className="stat-value" style={{ fontSize: 18 }}>{payload.user.email}</div>
        </div>
        <div>
          <div className="stat-label">Status</div>
          <div className="stat-value" style={{ fontSize: 18 }}>{payload.user.status}</div>
        </div>
        <div>
          <div className="stat-label">Requested At</div>
          <div className="stat-value" style={{ fontSize: 14 }}>{new Date(payload.user.requested_at * 1000).toLocaleString()}</div>
        </div>
      </div>
      <div className="card-header" style={{ marginTop: 20 }}>
        <h3>User Logs</h3>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Action</th>
            <th>Path</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {(payload.activity || []).map((item) => (
            <tr key={item.id}>
              <td>{new Date(item.timestamp * 1000).toLocaleString()}</td>
              <td>{item.action}</td>
              <td>{item.method} {item.path}</td>
              <td>{item.status_code || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AdminShell({ onLogout }: { onLogout: () => void }) {
  const navigate = useNavigate();
  const location = useLocation();
  const tabs = useMemo(() => [
    { to: '/admin/registered', label: 'Registered' },
    { to: '/admin/customers', label: 'Customers' },
    { to: '/admin/activity', label: 'Users Activity' },
    { to: '/admin/api-keys-health', label: 'API Keys Health' },
  ], []);

  const isUserDetail = /^\/admin\/user\/[^/]+$/i.test(location.pathname);
  const activeTab = isUserDetail
    ? 'detail'
    : location.pathname.startsWith('/admin/customers')
      ? 'customers'
      : location.pathname.startsWith('/admin/activity')
        ? 'activity'
        : location.pathname.startsWith('/admin/api-keys-health')
          ? 'api-keys-health'
          : 'registered';

  return (
    <div style={{ minHeight: '100vh', padding: 24, background: 'radial-gradient(circle at 10% 10%, rgba(59,130,246,0.2), transparent 45%), var(--bg-primary)' }}>
      <div className="card" style={{ borderColor: 'rgba(59,130,246,0.35)', boxShadow: '0 0 0 1px rgba(59,130,246,0.18) inset' }}>
        <div className="card-header">
          <div>
            <h3>Admin</h3>
            <div className="text-muted text-sm">Super Admin</div>
          </div>
          <button
            className="btn btn-secondary"
            type="button"
            onClick={() => {
              localStorage.removeItem('linktrade_admin_token');
              onLogout();
              navigate('/admin', { replace: true });
            }}
          >
            Logout
          </button>
        </div>
        <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
          {tabs.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={`btn ${
                (activeTab === 'customers' && tab.to.endsWith('/customers'))
                || (activeTab === 'activity' && tab.to.endsWith('/activity'))
                || (activeTab === 'api-keys-health' && tab.to.endsWith('/api-keys-health'))
                || (activeTab === 'registered' && tab.to.endsWith('/registered'))
                  ? 'btn-primary'
                  : 'btn-secondary'
              }`}
              style={{ minWidth: 140, justifyContent: 'center' }}
            >
              {tab.label}
            </NavLink>
          ))}
        </div>
      </div>
      {isUserDetail ? (
        <UserDetail />
      ) : activeTab === 'customers' ? (
        <CustomersTab />
      ) : activeTab === 'activity' ? (
        <ActivityTab />
      ) : activeTab === 'api-keys-health' ? (
        <ApiKeysHealthTab />
      ) : (
        <RegisteredTab />
      )}
    </div>
  );
}

export default function AdminPortal() {
  const [ready, setReady] = useState(false);
  const [valid, setValid] = useState(false);
  useEffect(() => {
    let active = true;
    const checkSession = async () => {
      try {
        await api.adminSession();
        if (active) setValid(true);
      } catch {
        if (active) setValid(false);
      } finally {
        if (active) setReady(true);
      }
    };
    void checkSession();
    const t = setInterval(() => { void checkSession(); }, 45000);
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        void checkSession();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      active = false;
      clearInterval(t);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  if (!ready) return <div className="card">Loading admin panel...</div>;
  if (!valid) return <AdminLogin onLoggedIn={() => setValid(true)} />;
  return <AdminShell onLogout={() => setValid(false)} />;
}
