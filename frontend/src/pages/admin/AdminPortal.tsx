import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, Routes, Route, NavLink } from 'react-router-dom';
import { api } from '@/utils/api';

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

function AdminLogin() {
  const navigate = useNavigate();
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('admin');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await api.adminLogin(username, password);
      localStorage.setItem('linktrade_admin_token', res.token);
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
}: {
  items: AccessItem[];
  actionLabel: string;
  onAction?: (userId: string) => void;
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
                <button className="btn btn-secondary btn-sm" onClick={() => navigate(`/admin/user/${item.user_id}`)}>
                  Open
                </button>
                {onAction && (
                  <button className="btn btn-primary btn-sm" onClick={() => onAction(item.user_id)}>
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
  const load = async () => {
    try {
      const res = await api.adminListAccessRequests('pending');
      setItems(res.items as AccessItem[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load registered users');
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      <div className="card-header">
        <h3>Registered (Pending Approval)</h3>
        <button className="btn btn-secondary btn-sm" onClick={() => void load()}>Refresh</button>
      </div>
      <AdminUsersTable
        items={items}
        actionLabel="Approve"
        onAction={(userId) => {
          void api.adminUpdateAccessRequest({ user_id: userId, status: 'approved' }).then(load);
        }}
      />
    </div>
  );
}

function CustomersTab() {
  const [items, setItems] = useState<AccessItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    try {
      const res = await api.adminCustomers();
      setItems(res.items as AccessItem[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load customers');
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      <div className="card-header">
        <h3>Customers (Approved)</h3>
        <button className="btn btn-secondary btn-sm" onClick={() => void load()}>Refresh</button>
      </div>
      <AdminUsersTable items={items} actionLabel="" />
    </div>
  );
}

function ActivityTab() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    try {
      const res = await api.adminActivity(200);
      setItems(res.activity as ActivityItem[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users activity');
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="card">
      {error && <div className="error-banner">{error}</div>}
      <div className="card-header">
        <h3>Users Activity</h3>
        <button className="btn btn-secondary btn-sm" onClick={() => void load()}>Refresh</button>
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

function UserDetail() {
  const { userId } = useParams();
  const [payload, setPayload] = useState<{ user?: AccessItem; activity?: ActivityItem[] }>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!userId) return;
    api.adminUserDetail(userId)
      .then((res) => setPayload({ user: res.user as AccessItem, activity: res.activity as ActivityItem[] }))
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load user detail'));
  }, [userId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!payload.user) return <div className="card">Loading user details...</div>;

  return (
    <div className="card">
      <div className="card-header">
        <h3>User Detail</h3>
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

function AdminShell() {
  const navigate = useNavigate();
  const tabs = useMemo(() => [
    { to: '/admin/registered', label: 'Registered' },
    { to: '/admin/customers', label: 'Customers' },
    { to: '/admin/activity', label: 'Users Activity' },
  ], []);

  return (
    <div style={{ minHeight: '100vh', padding: 24 }}>
      <div className="card">
        <div className="card-header">
          <div>
            <h3>Admin</h3>
            <div className="text-muted text-sm">Super Admin</div>
          </div>
          <button
            className="btn btn-secondary"
            onClick={() => {
              localStorage.removeItem('linktrade_admin_token');
              navigate('/admin', { replace: true });
            }}
          >
            Logout
          </button>
        </div>
        <div className="flex gap-2">
          {tabs.map((tab) => (
            <NavLink key={tab.to} to={tab.to} className={({ isActive }) => `btn ${isActive ? 'btn-primary' : 'btn-secondary'}`}>
              {tab.label}
            </NavLink>
          ))}
        </div>
      </div>
      <Routes>
        <Route path="registered" element={<RegisteredTab />} />
        <Route path="customers" element={<CustomersTab />} />
        <Route path="activity" element={<ActivityTab />} />
        <Route path="user/:userId" element={<UserDetail />} />
        <Route path="*" element={<RegisteredTab />} />
      </Routes>
    </div>
  );
}

export default function AdminPortal() {
  const [ready, setReady] = useState(false);
  const [valid, setValid] = useState(false);
  useEffect(() => {
    api.adminSession()
      .then(() => setValid(true))
      .catch(() => setValid(false))
      .finally(() => setReady(true));
  }, []);

  if (!ready) return <div className="card">Loading admin panel...</div>;
  if (!valid) return <AdminLogin />;
  return <AdminShell />;
}
