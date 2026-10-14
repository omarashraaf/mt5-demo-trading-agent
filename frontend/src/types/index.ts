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
  dedupe_key?: string;
  title: string;
  summary: string;
  timestamp_utc: number;
  event_type: string;
  category: string;
  country: string;
  importance: string;
  affected_assets: string[];
  raw_payload?: Record<string, unknown>;
  created_at?: number;
  usable?: boolean;
  usability_reason?: string;
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

export type PolicyMode = 'safe' | 'balanced' | 'aggressive' | 'custom';
export type GeminiRole = 'off' | 'advisory' | 'confirmation-required';

export interface UserPolicySettings {
  mode: PolicyMode;
  allowed_symbols: string[];
  max_risk_per_trade: number;
  max_daily_drawdown: number;
  max_weekly_loss?: number;
  max_drawdown?: number;
  max_open_trades: number;
  max_trades_per_symbol: number;
  max_correlated_exposure?: number;
  max_lot_size?: number;
  max_leverage_exposure?: number;
  max_trades_per_day?: number;
  max_margin_utilization: number;
  min_free_margin: number;
  min_reward_risk: number;
  max_spread?: number;
  min_confidence?: number;
  allowed_asset_classes?: string[];
  news_blackout_behavior?: 'block' | 'reduce_size' | 'warn' | string;
  auto_trading_allowed?: boolean;
  live_execution_allowed?: boolean;
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
  auto_trade_budget_mode?: 'full_margin' | 'custom_budget';
  auto_trade_budget_usd?: number | null;
}

export interface KillSwitchState {
  enabled: boolean;
  reason?: string;
  set_by?: string;
  set_at?: number;
}

export interface ApiKeyHealthItem {
  id: string;
  name: string;
  configured: boolean;
  enabled: boolean;
  available: boolean;
  degraded: boolean;
  state: string;
  credits_pct?: number | null;
  status: 'healthy' | 'warning' | 'degraded' | 'offline' | 'inactive' | 'low_credits' | string;
  reason?: string;
  checked_at: number;
}

export interface ApiKeysHealthResponse {
  checked_at: number;
  overall_status: 'healthy' | 'warning' | 'degraded' | string;
  unhealthy_count: number;
  warning_count?: number;
  items: ApiKeyHealthItem[];
  email_alerts?: {
    enabled: boolean;
    configured: boolean;
    last_alert_at?: number | null;
  };
}

export interface PolicySettingsResponse {
  user_policy: UserPolicySettings;
  presets: Partial<Record<PolicyMode, UserPolicySettings>>;
  runtime_controls: RuntimeControls;
  runtime_settings: Record<string, unknown>;
  universe?: UniverseSettings;
  event_providers?: {
    benzinga?: ProviderHealth;
  };
}

export type TradingPlatform = 'mt5' | 'ibkr' | 'tradestation';

export interface StatusResponse {
  connected: boolean;
  platform?: TradingPlatform;
  platforms_supported?: TradingPlatform[];
  service_mode?: 'lite' | 'pro' | 'auto' | string;
  background_services?: {
    cloud_pro_services_enabled?: boolean;
    auto_services_enabled?: boolean;
    cloud_sync_enabled?: boolean;
    cloud_brain_enabled?: boolean;
    cloud_archive_enabled?: boolean;
    meta_training_enabled?: boolean;
    scheduled_reconciliation_enabled?: boolean;
  };
  account: AccountInfo | null;
  terminal: TerminalInfo | null;
  last_error: string | null;
  is_demo: boolean;
  live_trading_enabled: boolean;
  panic_stop: boolean;
  kill_switch?: KillSwitchState;
  reconciliation?: ReconciliationReport;
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
  gemini_primary_model?: string;
  gemini_active_model?: string;
  gemini_using_fallback_model?: boolean;
  gemini_cooldown_seconds?: number;
  gemini_credits_pct?: number;
  user_policy?: UserPolicySettings;
  runtime_controls?: RuntimeControls;
  permissions?: {
    can_view_market_data?: boolean;
    can_create_strategy?: boolean;
    can_generate_signal?: boolean;
    can_paper_trade?: boolean;
    can_execute_demo?: boolean;
    can_execute_live?: boolean;
    can_enable_autotrader?: boolean;
    can_manage_risk?: boolean;
    can_access_logs?: boolean;
    can_access_research?: boolean;
    can_connect_broker?: boolean;
    can_manage_secrets?: boolean;
    can_access_admin?: boolean;
  };
  universe?: UniverseSettings;
  benzinga?: ProviderHealth;
  api_keys_health?: ApiKeysHealthResponse;
  portfolio?: PortfolioSnapshot;
  portfolio_targets?: {
    stop_loss_usd?: number | null;
    take_profit_usd?: number | null;
    updated_at?: number;
    close_inflight?: boolean;
    mode?: 'auto' | 'manual';
    last_trigger?: Record<string, unknown>;
  };
  publish_readiness?: {
    score: number;
    status: 'ready' | 'warning' | 'blocked' | string;
    blockers: string[];
    warnings: string[];
    checks?: Record<string, unknown>;
  };
  live_trading_readiness?: {
    status: 'ready' | 'warning' | 'blocked' | string;
    can_execute_live_orders: boolean;
    blockers: string[];
    warnings: string[];
    checks?: {
      platform?: string;
      connected?: boolean;
      account_is_demo?: boolean;
      backend_live_trading_enabled?: boolean;
      demo_only_default?: boolean;
      panic_stop?: boolean;
      trade_server_connected?: boolean;
      position_manager_running?: boolean;
      auto_trade_enabled?: boolean;
      auto_trade_session_armed?: boolean;
      gemini_available?: boolean;
      [key: string]: unknown;
    };
  };
  services?: Record<string, unknown>;
  auto_connect?: {
    pending: boolean;
    last_started_at?: number;
    last_finished_at?: number;
    last_error?: string;
    paused?: boolean;
    paused_at?: number;
    pause_reason?: string;
  };
}

export interface EventMarketRecommendation {
  symbol: string;
  score?: number;
  event_title?: string;
  event_type?: string;
  bias?: 'bullish' | 'bearish' | 'neutral' | string;
  event_risk?: 'low' | 'medium' | 'high' | string;
  news: NormalizedNewsItem[];
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

export interface CompiledStrategy {
  version: number;
  intent: string;
  symbols: string[];
  asset_classes?: string[];
  market_selection_mode?: string | null;
  market_region_hint?: string | null;
  entry_action: 'BUY' | 'SELL' | string;
  fixed_lot_size: number | null;
  allocation_pct_free_margin?: number | null;
  cadence_buy_every_seconds: number;
  cadence_sell_after_seconds: number;
  hold_for_seconds?: number;
  apply_under_any_conditions: boolean;
  policy_mode: PolicyMode;
  gemini_role: GeminiRole;
  scan_interval_seconds: number;
  min_reward_risk: number;
  human_summary?: string;
  assumptions?: string[];
  missing_details?: string[];
  warnings: string[];
  summary_lines: string[];
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
  time_msc?: number;
}

export interface ReconciliationIssue {
  code: string;
  severity: string;
  message: string;
  local_ticket?: number | null;
  broker_ticket?: number | null;
  details?: Record<string, unknown>;
}

export interface ReconciliationReport {
  checked_at: number;
  status: string;
  reason?: string;
  issue_count: number;
  issues: ReconciliationIssue[];
  local_open_count: number;
  broker_open_count: number;
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

export interface SimulatedExecutionResult {
  id: number;
  timestamp: number;
  order_id?: number | null;
  signal_id?: number | null;
  symbol: string;
  action: string;
  volume: number;
  price?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  execution_mode: 'paper' | 'shadow' | string;
  idempotency_key?: string;
  account_key?: string;
  execution_context?: string;
  result_json?: string;
}

export interface ExplainableTradeCard {
  signal_id: number;
  order_id?: number | null;
  symbol: string;
  market?: string;
  action: 'BUY' | 'SELL' | 'HOLD' | string;
  confidence?: number | null;
  risk_score?: number | null;
  risk_mode?: string;
  entry_reference_price?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  reward?: number | null;
  invalidation_level?: number | null;
  timeframe_alignment?: Record<string, unknown>;
  technical_evidence?: Record<string, unknown>;
  news_gemini_evidence?: Record<string, unknown>;
  disagreement_warnings?: unknown[];
  portfolio_exposure_impact?: Record<string, unknown>;
  broker_preflight_status?: {
    approved?: boolean;
    reason?: string;
  };
  final_decision?: string;
  block_reasons?: string[];
  execution_status?: Record<string, unknown>;
  model_strategy_version?: Record<string, unknown>;
  created_at?: number;
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
  close_pending?: boolean;
  close_order_id?: number | null;
  close_order_status?: string | null;
  estimated_margin?: number | null;
  started_with_usd?: number | null;
  entry_market_value_usd?: number | null;
  margin_used_usd?: number | null;
  margin_used_source?: 'entry_market_value' | 'broker_estimate' | string | null;
  budget_vs_margin_delta_usd?: number | null;
  budget_vs_margin_delta_pct?: number | null;
  sl_amount_usd?: number | null;
  tp_amount_usd?: number | null;
  target_management_mode?: 'automatic' | 'manual' | 'pending_auto' | string;
  target_management_updated_at?: number | null;
  signal_reason?: string | null;
  risk_reason?: string | null;
  expected_profit_time_minutes?: number | null;
  expected_profit_time_remaining_minutes?: number | null;
  expected_profit_eta_timestamp?: number | null;
  execution_trace?: {
    source?: string;
    reason?: string;
    policy_mode?: string;
    requested_agent_name?: string;
    active_agent_name?: string;
    amount_usd?: number | null;
    preflight_reason?: string;
    strategy?: {
      id?: string;
      name?: string;
      brief?: string;
      human_summary?: string;
      summary_lines?: string[];
    };
    gemini?: {
      state?: string;
      active_model?: string;
      available?: boolean;
      cooldown_seconds?: number;
    };
    meta_model?: {
      active?: boolean;
      version_id?: string;
    };
    signal_snapshot?: {
      agent_name?: string;
      confidence?: number;
      reason?: string;
      stop_loss?: number | null;
      take_profit?: number | null;
    };
  } | null;
}

export interface PortfolioTargetsResponse {
  stop_loss_usd?: number | null;
  take_profit_usd?: number | null;
  updated_at?: number;
  close_inflight?: boolean;
  mode?: 'auto' | 'manual';
  last_trigger?: Record<string, unknown>;
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
  news?: {
    sentiment: 'bullish' | 'bearish' | 'neutral' | string;
    impact: 'low' | 'medium' | 'high' | string;
    relevance: 'direct' | 'indirect' | 'global' | string;
    strength: number;
    risk_flag: boolean;
    message?: string;
    news_unavailable?: boolean;
  };
  execution_reason?: string;
  recommended_amount_usd?: number;
  recommended_amount_pct_free_margin?: number;
  commission_per_lot_side?: number | null;
  commission_round_turn_per_lot?: number | null;
  commission_model?: string;
  commission_percent_rate?: number | null;
  commission_notional_1lot?: number | null;
  commission_samples?: number | null;
  recommended_policy_mode?: PolicyMode | null;
  auto_execution_allowed_by_mode?: boolean;
  auto_execution_skipped_reason?: string;
  auto_execution_result?: OrderResult | null;
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
  blocking_reasons?: string[];
  risk_warnings?: string[];
  machine_reasons?: string[];
  portfolio_status?: string;
  allow_execute?: boolean;
  portfolio_fit_score?: number | null;
  projected_margin_utilization_pct?: number | null;
  projected_free_margin_pct?: number | null;
  gemini_state?: string;
  gemini_bias?: string;
  gemini_event_risk?: string;
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
  origin?: 'app_opened' | 'broker_imported' | 'mt5_connected_account';
  action: 'BUY' | 'SELL';
  status: 'open' | 'closed';
  opened_at: number;
  closed_at: number | null;
  duration_minutes: number | null;
  volume: number;
  entry_price: number | null;
  close_price?: number | null;
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
    executed_plan?: {
      entry_price?: number;
      stop_loss?: number;
      take_profit?: number;
      volume?: number;
      started_with_usd?: number;
      sl_amount_usd?: number;
      tp_amount_usd?: number;
      sl_pct_of_start?: number;
      tp_pct_of_start?: number;
    };
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
  audit_trail?: {
    time?: number | null;
    type: string;
    title: string;
    detail: string;
  }[];
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
  meta_model_inactive_reason?: string;
  active_approved_model?: ResearchModelVersion | null;
  last_training_run?: ResearchRunRecord | null;
  last_replay_run?: Record<string, unknown> | null;
  last_walk_forward_run?: Record<string, unknown> | null;
  best_candidate_model?: ResearchModelVersion | null;
  recent_attribution_reports?: Array<Record<string, unknown>>;
  model_quality?: {
    score: number;
    status: 'ready' | 'warning' | 'blocked' | string;
    blockers: string[];
    warnings: string[];
    checks?: Record<string, unknown>;
  };
  incremental_training?: {
    last_run_at: number;
    last_closed_count: number;
    local_closed_count?: number;
    clean_app_closed_count?: number;
  };
}
