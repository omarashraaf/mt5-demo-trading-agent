export interface AccountInfo {
  login: number;
  name: string;
  server: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  leverage: number;
  currency: string;
  trade_mode: number;
}

export interface TerminalInfo {
  connected: boolean;
  path: string;
  data_path: string;
  community_account: boolean;
  build: number;
  name: string;
}

export interface StatusResponse {
  connected: boolean;
  account: AccountInfo | null;
  terminal: TerminalInfo | null;
  last_error: string | null;
  is_demo: boolean;
  live_trading_enabled: boolean;
  panic_stop: boolean;
  active_agent: string;
}

export interface TickData {
  symbol: string;
  bid: number;
  ask: number;
  spread: number;
  time: number;
}

export interface BarData {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeSignal {
  action: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
  stop_loss: number | null;
  take_profit: number | null;
  max_holding_minutes: number | null;
  reason: string;
}

export interface RiskDecision {
  approved: boolean;
  reason: string;
  adjusted_volume: number;
  warnings: string[];
}

export interface EvaluateResponse {
  signal: TradeSignal;
  signal_id: number | null;
  risk_decision: RiskDecision;
  agent_name: string;
}

export interface OrderResult {
  success: boolean;
  retcode: number;
  retcode_desc: string;
  ticket: number | null;
  volume: number | null;
  price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  comment: string;
}

export interface PositionInfo {
  ticket: number;
  symbol: string;
  type: string;
  volume: number;
  price_open: number;
  price_current: number;
  stop_loss: number;
  take_profit: number;
  profit: number;
  time: number;
  comment: string;
}

export interface RiskSettings {
  risk_percent_per_trade: number;
  max_daily_loss_percent: number;
  max_concurrent_positions: number;
  min_confidence_threshold: number;
  max_spread_threshold: number;
  allowed_symbols: string[];
  require_stop_loss: boolean;
  use_fixed_lot: boolean;
  fixed_lot_size: number;
  auto_trade_enabled: boolean;
  auto_trade_min_confidence: number;
  auto_trade_scan_interval_seconds: number;
}

export interface AgentInfo {
  name: string;
  description: string;
}

export interface LogEntry {
  id: number;
  timestamp: number;
  log_type: string;
  [key: string]: unknown;
}

export interface SavedCredentials {
  id: number;
  account: number;
  server: string;
  password: string;
  terminal_path: string;
  label: string;
  created_at: number;
  last_used: number | null;
}

export interface Recommendation {
  symbol: string;
  signal: TradeSignal;
  signal_id: number | null;
  risk_decision: RiskDecision;
  entry_price_estimate: number;
  explanation: string;
  ready_to_execute: boolean;
  category?: string;
  description?: string;
}

export interface SmartEvaluateResponse {
  recommendations: Recommendation[];
  scanned_at: number;
}

export interface AIActivity {
  timestamp: number;
  action: string;
  symbol: string;
  ticket: number;
  detail: string;
  profit: number;
  source?: string;  // "scanner" or "position_manager"
  // Scanner-specific fields
  confidence?: number;
  success?: boolean;
}
