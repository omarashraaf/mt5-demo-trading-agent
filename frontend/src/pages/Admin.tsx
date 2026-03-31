import { useEffect, useState } from 'react';
import { api } from '@/utils/api';

type AdminUser = {
  id?: string;
  email?: string;
  app_metadata?: { role?: string };
  created_at?: string;
  last_sign_in_at?: string;
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
  details_json: Record<string, unknown>;
};

export default function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createEmail, setCreateEmail] = useState('');
  const [createPassword, setCreatePassword] = useState('');
  const [createRole, setCreateRole] = useState<'admin' | 'user'>('user');

  const loadAll = async () => {
    setLoading(true);
    setError(null);
    try {
      const [usersRes, activityRes] = await Promise.all([api.adminListUsers(), api.adminActivity(100)]);
      setUsers((usersRes.users || []) as AdminUser[]);
      setActivity((activityRes.activity || []) as ActivityItem[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load admin data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadAll();
  }, []);

  const createUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.adminCreateUser({ email: createEmail.trim(), password: createPassword, role: createRole });
      setCreateEmail('');
      setCreatePassword('');
      setCreateRole('user');
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user');
    } finally {
      setBusy(false);
    }
  };

  const updateRole = async (userId: string, role: 'admin' | 'user') => {
    setBusy(true);
    setError(null);
    try {
      await api.adminUpdateRole({ user_id: userId, role });
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update role');
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="card">Loading admin panel...</div>;
  }

  return (
    <div>
      <div className="page-header">
        <h2>Admin Panel</h2>
        <p>Manage users and monitor activity.</p>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="card-header">
          <h3>Create User</h3>
        </div>
        <form className="grid-3" onSubmit={createUser}>
          <div className="form-group">
            <label>Email</label>
            <input className="form-input" value={createEmail} onChange={(e) => setCreateEmail(e.target.value)} required />
          </div>
          <div className="form-group">
            <label>Password</label>
            <input className="form-input" type="password" minLength={6} value={createPassword} onChange={(e) => setCreatePassword(e.target.value)} required />
          </div>
          <div className="form-group">
            <label>Role</label>
            <select className="form-input" value={createRole} onChange={(e) => setCreateRole(e.target.value as 'admin' | 'user')}>
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          <button className="btn btn-primary" type="submit" disabled={busy}>Create</button>
        </form>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Users ({users.length})</h3>
          <button className="btn btn-secondary btn-sm" onClick={() => void loadAll()}>Refresh</button>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Created</th>
              <th>Last Login</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const role = String(user.app_metadata?.role || 'user').toLowerCase();
              return (
                <tr key={user.id || user.email}>
                  <td>{user.email || '-'}</td>
                  <td>{role}</td>
                  <td>{user.created_at ? new Date(user.created_at).toLocaleString() : '-'}</td>
                  <td>{user.last_sign_in_at ? new Date(user.last_sign_in_at).toLocaleString() : '-'}</td>
                  <td>
                    {user.id && (
                      <div className="flex gap-2">
                        <button className="btn btn-secondary btn-sm" disabled={busy || role === 'admin'} onClick={() => void updateRole(user.id as string, 'admin')}>
                          Make admin
                        </button>
                        <button className="btn btn-secondary btn-sm" disabled={busy || role === 'user'} onClick={() => void updateRole(user.id as string, 'user')}>
                          Make user
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>User Activity ({activity.length})</h3>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Time</th>
              <th>User</th>
              <th>Role</th>
              <th>Action</th>
              <th>Path</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {activity.map((item) => (
              <tr key={item.id}>
                <td>{new Date(item.timestamp * 1000).toLocaleString()}</td>
                <td>{item.user_email || '-'}</td>
                <td>{item.role || 'user'}</td>
                <td>{item.action}</td>
                <td>{item.method} {item.path}</td>
                <td>{item.status_code || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
