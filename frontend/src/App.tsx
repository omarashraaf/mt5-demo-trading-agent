import { Routes, Route, NavLink } from 'react-router-dom';
import { useState, useCallback, useEffect } from 'react';
import {
  LayoutDashboard,
  Plug,
  BarChart3,
  Brain,
  Shield,
  Zap,
  Activity,
  MessageSquare,
  History,
  ScrollText,
  ChevronDown,
  ChevronRight,
  Settings,
  Newspaper,
} from 'lucide-react';
import { api } from './utils/api';
import type { StatusResponse } from './types';
import SimpleDashboard from './pages/SimpleDashboard';
import Connection from './pages/Connection';
import Market from './pages/Market';
import Strategy from './pages/Strategy';
import RiskManagement from './pages/RiskManagement';
import Execution from './pages/Execution';
import Logs from './pages/Logs';
import AIActivityPage from './pages/AIActivity';
import ChatPage from './pages/Chat';
import TradeHistoryPage from './pages/TradeHistory';
import EventsPage from './pages/Events';
import AuthPage from './pages/Auth';
import { useAuth } from './context/AuthContext';

const ADVANCED_NAV = [
  { path: '/chat', label: 'Gemini Chat', icon: MessageSquare },
  { path: '/ai-activity', label: 'AI Brain Activity', icon: Activity },
  { path: '/market', label: 'Market Data', icon: BarChart3 },
  { path: '/strategy', label: 'Strategy', icon: Brain },
  { path: '/risk', label: 'Risk Settings', icon: Shield },
  { path: '/execution', label: 'Execution', icon: Zap },
  { path: '/trade-history', label: 'Trade History', icon: History },
  { path: '/events', label: 'News Feed', icon: Newspaper },
  { path: '/logs', label: 'Logs', icon: ScrollText },
];

export default function App() {
  const { loading: authLoading, user, signOut } = useAuth();
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(() => {
    return localStorage.getItem('advancedNav') === 'true';
  });

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.getStatus();
      setStatus(s);
    } catch {
      // Keep last known status on transient API timeouts to avoid UI flicker
      // between dashboard and connection state.
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    const timer = setInterval(refreshStatus, 8000);
    return () => clearInterval(timer);
  }, [refreshStatus]);

  const toggleAdvanced = () => {
    setAdvancedOpen((prev) => {
      localStorage.setItem('advancedNav', String(!prev));
      return !prev;
    });
  };

  const connected = Boolean(status?.connected && status?.account);
  const authed = Boolean(user);

  if (authLoading) {
    return (
      <div className="app-layout" style={{ alignItems: 'center', justifyContent: 'center' }}>
        <div className="card" style={{ marginBottom: 0 }}>
          Checking session...
        </div>
      </div>
    );
  }

  if (!authed) {
    return <AuthPage />;
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>TRADING ASSISTANT</h1>
          <div className="subtitle">
            {connected ? (
              <span style={{ color: 'var(--accent-green)' }}>
                {status?.account?.name || `Account ${status?.account?.login}`}
              </span>
            ) : (
              'Demo Mode'
            )}
          </div>
          <div style={{ marginTop: 10 }}>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => void signOut()}
            >
              Logout
            </button>
          </div>
        </div>
        <nav className="sidebar-nav">
          <NavLink
            to="/"
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
            end
          >
            <LayoutDashboard size={16} />
            Dashboard
          </NavLink>

          <NavLink
            to="/connection"
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
          >
            <Plug size={16} />
            {connected ? 'Account' : 'Connect'}
          </NavLink>

          <NavLink
            to="/trade-history"
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
          >
            <History size={16} />
            Trade History
          </NavLink>

          {/* Advanced section - hidden by default */}
          <div
            className="nav-item"
            onClick={toggleAdvanced}
            style={{
              marginTop: 16,
              cursor: 'pointer',
              userSelect: 'none',
              color: 'var(--text-muted)',
              fontSize: 11,
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}
          >
            <Settings size={12} />
            {advancedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            Advanced
          </div>

          {advancedOpen && ADVANCED_NAV.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
              style={{ paddingLeft: 28, fontSize: 12 }}
            >
              <item.icon size={13} />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="flex items-center gap-2 text-sm">
            <span className={`status-dot ${connected ? 'connected' : 'disconnected'}`} />
            {connected ? (
              <span className="text-green" style={{ fontSize: 12 }}>Connected</span>
            ) : (
              <span className="text-muted" style={{ fontSize: 12 }}>Not connected</span>
            )}
          </div>
          {status?.panic_stop && (
            <div className="badge badge-red mt-2" style={{ fontSize: 10 }}>TRADING PAUSED</div>
          )}
        </div>
      </aside>

      <main className="main-content">
        <div className="warning-banner">
          This is a demo account. Practice trading with virtual money. Results here do not guarantee real profits.
        </div>

        <Routes>
          <Route path="/" element={<SimpleDashboard status={status} onRefresh={refreshStatus} />} />
          <Route path="/connection" element={<Connection status={status} onRefresh={refreshStatus} />} />
          <Route path="/market" element={<Market connected={connected} />} />
          <Route path="/strategy" element={<Strategy connected={connected} />} />
          <Route path="/risk" element={<RiskManagement status={status} />} />
          <Route path="/execution" element={<Execution connected={connected} status={status} />} />
          <Route path="/trade-history" element={<TradeHistoryPage connected={connected} />} />
          <Route path="/ai-activity" element={<AIActivityPage status={status} />} />
          <Route path="/chat" element={<ChatPage connected={connected} />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/events" element={<EventsPage />} />
        </Routes>
      </main>
    </div>
  );
}
