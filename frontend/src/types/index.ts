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
  partial_news_only?: boolean;
  status?: 'healthy' | 'news_only' | 'degraded';
  reason?: string;
  sample_count?: number;
  calendar_count?: number;
  capabilities?: {
    market_news?: boolean;
    economic_calendar?: boolean;
  };
}

export interface ExternalEventRecord {
  id: number;
  source: string;
  source_event_id: string;
  title: string;
  summary: string;
  timestamp_utc: number;
  event_type: string;
  category: string;
  country: string;
  importance: string;
  affected_assets: string[];
}

export interface EventAssetMappingRecord {
  id: number;
  external_event_id: number;
  symbol: string;
  baseline_bias: string;
  needs_gemini_clarification: number;
  tradable: number;
  mapping_score: number;
  reason: string;
  created_at: number;
}

export interface GeminiEventAssessmentRecord {
  id: number;
  external_event_id: number;
  event_type: string;
  affected_assets_json: string;
  importance: string;
  bias_by_asset_json: string;
  persistence_horizon: string;
  event_risk: string;
  confidence_adjustment: number;
  contradiction_flag: number;
  summary_reason: string;
  degraded: number;
  error: string | null;
  created_at: number;
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
  platform?: 'mt5' | 'ibkr';
  platforms_supported?: Array<'mt5' | 'ibkr'>;
  account: AccountInfo | null;
  terminal: TerminalInfo | null;
  last_error: string | null;
  is_demo: boolean;
  live_trading_enabled: boolean;
  panic_stop: boolean;
  active_agent: string;
  active_strategy?: {
    id: string;
    source?: string;
  };
  credential_storage_available?: boolean;
  gemini_available?: boolean;
  gemini_degraded?: boolean;
  gemini_last_error?: string | null;
  gemini_state?: 'available' | 'degraded' | 'cooldown' | 'unavailable' | string;
  gemini_cooldown_seconds?: number;
  gemini_credits_pct?: number;
  user_policy?: UserPolicySettings;
  runtime_controls?: RuntimeControls;
  universe?: UniverseSettings;
  finnhub?: ProviderHealth;
  portfolio?: PortfolioSnapshot;
  services?: Record<string, unknown>;
}

export interface StrategyProfile {
  id: string;
  name: string;
  description: string;
  brief?: string;
  scope: 'builtin' | 'custom' | string;
  is_default: boolean;
  is_selected: boolean;
  config: Record<string, unknown>;
}

export interface StrategiesResponse {
  active_strategy_id: string;
  items: StrategyProfile[];
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
  recommended_amount_usd?: number;
  recommended_amount_pct_free_margin?: number;
  commission_per_lot_side?: number | null;
  commission_round_turn_per_lot?: number | null;
  commission_model?: string;
  commission_percent_rate?: number | null;
  commission_notional_1lot?: number | null;
  commission_samples?: number | null;
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
  signal_id?: number | null;
  detail: string;
  profit: number;
  profit_pct?: number | null;
  decision_reason?: string;
  gemini_summary?: string;
  meta_model_summary?: string;
  source?: string;
  confidence?: number;
  success?: boolean;
  quality_score?: number;
}

export interface ChatMessageItem {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatTradeRequest {
  symbol: string;
  action: 'BUY' | 'SELL';
  amount_usd: number;
  stop_loss?: number | null;
  take_profit?: number | null;
  reason?: string | null;
}

export interface ChatTradePreview {
  symbol: string;
  action: 'BUY' | 'SELL';
  amount_usd: number;
  estimated_entry: number;
  estimated_volume: number;
  stop_loss: number | null;
  take_profit: number | null;
  reason?: string | null;
}

export interface ChatResponse {
  reply: string;
  intent: 'chat' | 'trade_request';
  trade_request?: ChatTradeRequest | null;
  trade_preview?: ChatTradePreview | null;
  order_result?: OrderResult | null;
  executed: boolean;
}

export interface ChatHistoryResponse {
  messages: ChatMessageItem[];
}

export interface TradeHistorySummary {
  total_trades: number;
  closed_trades: number;
  open_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate_pct: number;
  total_profit_usd: number;
  avg_profit_per_closed_trade_usd: number;
  total_started_capital_usd: number;
  roi_pct: number | null;
  best_trade_usd: number;
  worst_trade_usd: number;
}

export interface TradeHistoryItem {
  ticket: number | null;
  signal_id: number | null;
  symbol: string;
  action: 'BUY' | 'SELL';
  status: 'open' | 'closed';
  opened_at: number;
  closed_at: number | null;
  duration_minutes: number | null;
  volume: number;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  profit_usd: number | null;
  profit_pct: number | null;
  started_with_usd: number | null;
  ended_with_usd: number | null;
  entry_market_value_usd: number | null;
  sl_amount_usd: number | null;
  tp_amount_usd: number | null;
  sl_pct_of_start: number | null;
  tp_pct_of_start: number | null;
  started_with_source: 'provided' | 'estimated' | 'unknown';
  agent_name?: string;
  signal_confidence?: number;
  signal_reason?: string;
  risk_approved?: number;
  risk_reason?: string;
  decision_reason?: string | null;
  trade_reasons?: string[];
  gemini_response?: string | null;
  meta_model?: {
    version_id: string;
    profit_probability: number;
    expected_edge: number;
    blocked: boolean;
    changed_decision: boolean;
    quality_before: number;
    quality_after: number;
  } | null;
  post_analysis?: {
    summary?: string;
    what_went_well?: string[];
    mistakes?: string[];
    improvement_actions?: string[];
    root_causes?: string[];
    future_confidence_adjustment?: number;
    quality_scores?: {
      execution_quality?: number;
      risk_discipline?: number;
      timing_quality?: number;
    };
    diagnostics?: {
      late_entry_flag?: number;
      spread_stress_flag?: number;
      trend_conflict_flag?: number;
      risk_overexposure_flag?: number;
      stop_too_tight_flag?: number;
      target_too_far_flag?: number;
      news_shock_flag?: number;
      execution_delay_flag?: number;
    };
    recommendation?: {
      expected_win_rate_adjustment?: number;
      suggested_sl_pct_of_start?: number;
      suggested_tp_pct_of_start?: number;
      next_trade_size_adjustment_pct?: number;
    };
    generated_by?: string;
    generated_at?: number;
  } | null;
  exit_reason?: string | null;
}

export interface TradeHistoryResponse {
  summary: TradeHistorySummary;
  trades: TradeHistoryItem[];
}

export interface ResearchModelVersion {
  version_id: string;
  algorithm: string;
  target_definition: string;
  feature_schema_version: string;
  training_date: number;
  data_range_start?: number | null;
  data_range_end?: number | null;
  evaluation_metrics_json: Record<string, unknown>;
  walk_forward_metrics_json: Record<string, unknown>;
  approval_status: 'training' | 'candidate' | 'approved' | 'rejected' | 'archived';
  notes?: string;
}

export interface ResearchRunRecord {
  run_id: string;
  version_id?: string | null;
  started_at: number;
  finished_at?: number | null;
  status: string;
  metrics_json?: Record<string, unknown>;
  params_json?: Record<string, unknown>;
  notes?: string;
}

export interface ResearchReportRecord {
  report_id: string;
  report_type: string;
  generated_at: number;
  report_json: Record<string, unknown>;
}

export interface ResearchStatusResponse {
  enabled: boolean;
  config: {
    AUTO_TRAIN_ON_DEMO: boolean;
    AUTO_PROMOTE_ON_DEMO: boolean;
    MIN_TRADES_BEFORE_TRAINING: number;
    TRAINING_WINDOW_DAYS: number;
    WALK_FORWARD_WINDOWS: number;
  };
  meta_model_active: boolean;
  meta_model_active_version: string;
  active_approved_model?: ResearchModelVersion | null;
  last_training_run?: ResearchRunRecord | null;
  last_replay_run?: Record<string, unknown> | null;
  last_walk_forward_run?: Record<string, unknown> | null;
  best_candidate_model?: ResearchModelVersion | null;
  recent_attribution_reports?: Array<Record<string, unknown>>;
  incremental_training?: {
    last_run_at: number;
    last_closed_count: number;
  };
}
