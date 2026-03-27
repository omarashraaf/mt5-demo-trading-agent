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

export interface PortfolioSnapshot {
  margin_utilization_pct: number;
  free_margin_pct: number;
  open_positions_total: number;
  exposure_by_symbol: Record<string, number>;
  exposure_by_category: Record<string, number>;
  exposure_by_sector: Record<string, number>;
  usd_beta_exposure_pct: number;
  stocks_equity_exposure_pct: number;
}

export interface UniverseSettings {
  mode: string;
  active_asset_classes: string[];
  enabled_symbols: string[];
  disabled_symbols: string[];
  symbol_profile_overrides: Record<string, string>;
  selection_mode?: string;
}

export interface ProviderHealth {
  provider: string;
  enabled: boolean;
  available: boolean;
  degraded: boolean;
  reason?: string;
  sample_count?: number;
}

export type PolicyMode = 'safe' | 'balanced' | 'aggressive';
export type GeminiRole = 'off' | 'advisory' | 'confirmation-required';

export interface UserPolicySettings {
  mode: PolicyMode;
  allowed_symbols: string[];
  max_risk_per_trade: number;
  max_daily_drawdown: number;
  max_open_trades: number;
  max_trades_per_symbol: number;
  max_margin_utilization: number;
  min_free_margin: number;
  min_reward_risk: number;
  allow_counter_trend_trades: boolean;
  allow_overnight_holding: boolean;
  gemini_role: GeminiRole;
  session_filters: string[];
  demo_only_default: boolean;
}

export interface RuntimeControls {
  auto_trade_enabled: boolean;
  auto_trade_scan_interval_seconds: number;
  panic_stop: boolean;
}

export interface PolicySettingsResponse {
  user_policy: UserPolicySettings;
  presets: Record<PolicyMode, UserPolicySettings>;
  runtime_controls: RuntimeControls;
  runtime_settings: Record<string, unknown>;
  universe?: UniverseSettings;
  event_providers?: {
    finnhub?: ProviderHealth;
  };
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
  credential_storage_available?: boolean;
  gemini_available?: boolean;
  gemini_degraded?: boolean;
  gemini_last_error?: string | null;
  user_policy?: UserPolicySettings;
  runtime_controls?: RuntimeControls;
  universe?: UniverseSettings;
  finnhub?: ProviderHealth;
  portfolio?: PortfolioSnapshot;
  services?: Record<string, unknown>;
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
  metadata?: Record<string, unknown>;
}

export interface RiskDecision {
  approved: boolean;
  reason: string;
  adjusted_volume: number;
  warnings: string[];
  status?: 'pass' | 'warn' | 'block';
  machine_reasons?: string[];
  metrics_snapshot?: Record<string, unknown>;
}

export interface GeminiAssessment {
  used?: boolean;
  available?: boolean;
  degraded?: boolean;
  news_bias?: 'bullish' | 'bearish' | 'neutral';
  macro_relevance?: 'low' | 'medium' | 'high';
  event_risk?: 'low' | 'medium' | 'high';
  confidence_adjustment?: number;
  contradiction_flag?: boolean;
  summary_reason?: string;
  source_quality_score?: number;
  reason?: string;
  error?: string | null;
}

export interface TradeQualityAssessment {
  trend_alignment_score: number;
  momentum_quality_score: number;
  entry_timing_score: number;
  volatility_quality_score: number;
  reward_risk_score: number;
  spread_quality_score: number;
  portfolio_fit_score: number;
  news_alignment_score: number;
  contradiction_penalty: number;
  final_trade_quality_score: number;
  threshold: number;
  no_trade_zone: boolean;
  no_trade_reasons: string[];
  summary: string;
}

export interface PortfolioRiskAssessment {
  status: 'pass' | 'warn' | 'block';
  allow_execute: boolean;
  reason: string;
  blocking_reasons: string[];
  warnings: string[];
  metrics_snapshot: Record<string, unknown>;
  correlated_symbols: string[];
  margin_required: number;
  projected_margin_utilization_pct: number;
  projected_free_margin_pct: number;
  portfolio_fit_score: number;
}

export interface AntiChurnAssessment {
  blocked: boolean;
  threshold_boost: number;
  reasons: string[];
  metadata?: Record<string, unknown>;
}

export interface PositionManagementPlan {
  manage_position: boolean;
  strategy?: string | null;
  max_holding_minutes?: number | null;
  planned_hold_minutes?: number | null;
  initial_thesis?: string;
  expected_hold_minutes?: number | null;
  atr_regime?: string;
  close_before_session_end?: boolean;
  session_close_buffer_minutes?: number;
  stale_after_minutes?: number | null;
  min_progress_r_multiple?: number;
  target_progress_r_multiple?: number;
  break_even_activation_rule?: string;
  trailing_rule?: string;
  invalidation_condition?: string;
  time_stop_rule?: string;
  notes?: string[];
  metadata?: Record<string, unknown>;
}

export interface EvaluateResponse {
  signal: TradeSignal;
  signal_id: number | null;
  risk_decision: RiskDecision;
  agent_name: string;
  degraded_reasons?: string[];
  gemini_confirmation?: GeminiAssessment | null;
  trade_quality?: TradeQualityAssessment;
  portfolio_risk?: PortfolioRiskAssessment;
  anti_churn?: AntiChurnAssessment;
  execution_reason?: string;
  position_management_plan?: PositionManagementPlan;
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
  secret_ref?: string;
  secret_backend?: string;
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
  degraded_reasons?: string[];
  trade_quality?: TradeQualityAssessment;
  portfolio_risk?: PortfolioRiskAssessment;
  anti_churn?: AntiChurnAssessment;
  gemini_confirmation?: GeminiAssessment | null;
  execution_reason?: string;
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
  source?: string;
  confidence?: number;
  success?: boolean;
  quality_score?: number;
}
