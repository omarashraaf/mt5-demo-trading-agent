import { runtimeConfig } from '@/config';

export default function PortalDashboard({ isAdmin }: { isAdmin: boolean }) {
  return (
    <div style={{ minHeight: '100vh', padding: 24, background: 'radial-gradient(circle at 20% 20%, rgba(59,130,246,0.18), transparent 45%), var(--bg-primary)' }}>
      <div className="card" style={{ borderColor: 'rgba(59,130,246,0.35)', boxShadow: '0 0 0 1px rgba(59,130,246,0.18) inset' }}>
        <div className="page-header">
          <h2>LinkTrade Portal</h2>
          <p>Cloud-authenticated access. Trading runtime stays on your local machine.</p>
        </div>
        <div className="grid-2">
          <div className="card" style={{ marginBottom: 0 }}>
            <h3 style={{ marginBottom: 10 }}>Local Runtime</h3>
            <p className="text-muted text-sm" style={{ marginBottom: 14 }}>
              Open the local trading runtime to connect MT5/IBKR and run execution safely on your device.
            </p>
            <a className="btn btn-primary" href={runtimeConfig.localRuntimeUrl} target="_blank" rel="noreferrer">
              Open Localhost Runtime
            </a>
          </div>
          <div className="card" style={{ marginBottom: 0 }}>
            <h3 style={{ marginBottom: 10 }}>Admin Panel</h3>
            <p className="text-muted text-sm" style={{ marginBottom: 14 }}>
              Manage approvals, customers, and activity from the cloud admin console.
            </p>
            {isAdmin ? (
              <a className="btn btn-secondary" href="/admin">
                Open Admin
              </a>
            ) : (
              <span className="badge badge-yellow">Admin only</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
