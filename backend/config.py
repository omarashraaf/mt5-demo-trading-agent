from __future__ import annotations

import json
import os
from collections.abc import Mapping

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

load_dotenv()


def _read_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_csv(
    value: str | list[str] | tuple[str, ...] | None,
    *,
    default: list[str] | None = None,
) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_json_map(value: str | Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key).strip().upper(): str(val).strip() for key, val in value.items() if str(key).strip()}
    cleaned = value.strip()
    if not cleaned:
        return {}
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("SYMBOL_PROFILE_OVERRIDES must be a JSON object.")
    return {str(key).strip().upper(): str(val).strip() for key, val in data.items() if str(key).strip()}


def _resolve_db_path(value: str) -> str:
    """Resolve DB path with backward-compatible fallback.

    For relative DB paths, prefer backend-module-relative resolution first so
    runtime behavior is stable regardless of process working directory.
    If no module-relative DB exists, keep backward-compatible cwd/parent fallback.
    """
    cleaned = (value or "trading_agent.db").strip().strip('"').strip("'")
    if not cleaned:
        cleaned = "trading_agent.db"
    if os.path.isabs(cleaned):
        return cleaned

    module_dir = os.path.dirname(os.path.abspath(__file__))
    module_candidate = os.path.abspath(os.path.join(module_dir, cleaned))
    if os.path.exists(module_candidate):
        return module_candidate

    primary = os.path.abspath(cleaned)
    if os.path.exists(primary):
        return primary

    parent_candidate = os.path.abspath(os.path.join(os.getcwd(), "..", cleaned))
    if os.path.exists(parent_candidate):
        return parent_candidate

    return primary


class AppConfig(BaseModel):
    APP_MODE: str = "development"
    APP_SERVICE_MODE: str = "lite"
    LIVE_TRADING_ENABLED: bool = False
    EXECUTION_MODE: str = "live"
    AUTO_TRADER_EXECUTION_MODE: str = "shadow"
    DEFAULT_TERMINAL_PATH: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    DB_PATH: str = "trading_agent.db"
    LOG_LEVEL: str = "INFO"
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    GEMINI_MODEL: str = "gemma-4-26b-a4b-it"
    GEMINI_TIMEOUT_SECONDS: float = Field(default=12.0, gt=1.0, le=60.0)
    GEMINI_MAX_RETRIES: int = Field(default=1, ge=0, le=3)
    SMART_EVAL_PER_SYMBOL_TIMEOUT_SECONDS: float = Field(default=14.0, gt=2.0, le=90.0)
    SMART_EVAL_TOTAL_TIMEOUT_SECONDS: float = Field(default=60.0, gt=5.0, le=180.0)
    SMART_EVAL_MAX_CONCURRENT_EVALS: int = Field(default=2, ge=1, le=8)
    SMART_EVAL_MAX_SCAN_SYMBOLS_AUTO: int = Field(default=8, ge=2, le=40)
    SMART_EVAL_MAX_SCAN_SYMBOLS_MANUAL: int = Field(default=60, ge=4, le=60)
    MARKET_TICK_STREAM_INTERVAL_MS: int = Field(default=25, ge=10, le=1000)
    SAVE_CREDENTIALS_BY_DEFAULT: bool = False
    REQUIRE_SECURE_CREDENTIAL_STORAGE: bool = True
    ENABLE_BENZINGA: bool = False
    BENZINGA_API_KEY: str = ""
    BENZINGA_TIMEOUT_SECONDS: float = Field(default=8.0, gt=1.0, le=30.0)
    BENZINGA_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    ACTIVE_ASSET_CLASSES: list[str] = Field(default_factory=lambda: ["Forex", "Indices", "Commodities", "Stocks", "Crypto"])
    ENABLED_SYMBOLS: list[str] = Field(default_factory=list)
    AUTO_TRADE_FALLBACK_SYMBOLS: list[str] = Field(
        default_factory=lambda: [
            "US500",
            "US100",
            "US30",
            "GER40",
            "AAPL",
            "MSFT",
            "NVDA",
            "AMD",
            "AMZN",
            "TSLA",
            "GOLD",
            "SILVER",
            "WTI",
            "BRENT",
        ]
    )
    AUTO_TRADE_MAX_SINGLE_TRADE_BUDGET_PCT: float = Field(default=25.0, ge=1.0, le=100.0)
    DISABLED_SYMBOLS: list[str] = Field(default_factory=lambda: ["NATGAS", "XNGUSD", "NGAS", "NG"])
    SYMBOL_PROFILE_OVERRIDES: dict[str, str] = Field(
        default_factory=lambda: {
            "US500": "indices",
            "US100": "indices",
            "US30": "indices",
            "GER40": "indices",
            "GOLD": "gold",
            "WTI": "commodities",
            "BRENT": "commodities",
        }
    )
    ENABLE_RESEARCH_CYCLE: bool = False
    AUTO_TRAIN_ON_DEMO: bool = False
    AUTO_PROMOTE_ON_DEMO: bool = False
    MIN_TRADES_BEFORE_TRAINING: int = Field(default=50, ge=10, le=100000)
    TRAINING_WINDOW_DAYS: int = Field(default=90, ge=7, le=3650)
    WALK_FORWARD_WINDOWS: int = Field(default=5, ge=2, le=50)
    AUTO_META_TRAINING_ENABLED: bool = True
    AUTO_META_TRAIN_INTERVAL_SECONDS: int = Field(default=900, ge=60, le=86400)
    AUTO_META_TRAIN_MIN_CLOSED_TRADES: int = Field(default=20, ge=10, le=100000)
    AUTO_META_AUTO_APPROVE: bool = True
    AUTO_META_MIN_PRECISION: float = Field(default=0.50, ge=0.0, le=1.0)
    AUTO_META_MIN_F1: float = Field(default=0.45, ge=0.0, le=1.0)
    AUTO_META_MIN_POSITIVE_SAMPLES: int = Field(default=12, ge=1, le=100000)
    AUTO_META_TARGET_COLUMN: str = "profitable_after_costs_90m"
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    AUTH_REQUIRED: bool = False
    ENABLE_ADMIN_BOOTSTRAP: bool = True
    ADMIN_BOOTSTRAP_USERNAME: str = "admin"
    ADMIN_BOOTSTRAP_PASSWORD: str = "admin"
    SECRET_STORAGE_MODE: str = "keyring"
    SECRET_ENCRYPTION_KEY: str = ""
    CLOUD_SYNC_ENABLED: bool = True
    CLOUD_LOG_TABLE: str = "runtime_logs"
    CLOUD_SYNC_TIMEOUT_SECONDS: float = Field(default=8.0, gt=1.0, le=30.0)
    CLOUD_ARCHIVE_ENABLED: bool = True
    CLOUD_ARCHIVE_INTERVAL_SECONDS: int = Field(default=900, ge=60, le=86400)
    CLOUD_BRAIN_ENABLED: bool = True
    CLOUD_BRAIN_TABLE: str = "brain_commands"
    CLOUD_BRAIN_POLL_SECONDS: int = Field(default=8, ge=3, le=300)
    CLOUD_BRAIN_DECISION_ENABLED: bool = False
    CLOUD_BRAIN_DECISION_URL: str = ""
    CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS: float = Field(default=6.0, gt=1.0, le=30.0)
    CLOUD_SECRETS_ENABLED: bool = True
    CLOUD_SECRETS_TABLE: str = "app_secrets"
    API_HEALTH_ALERTS_ENABLED: bool = False
    API_HEALTH_ALERT_INTERVAL_SECONDS: int = Field(default=60, ge=15, le=3600)
    API_HEALTH_ALERT_COOLDOWN_SECONDS: int = Field(default=900, ge=60, le=86400)
    API_HEALTH_SMTP_HOST: str = "smtp.gmail.com"
    API_HEALTH_SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    API_HEALTH_SMTP_USER: str = ""
    API_HEALTH_SMTP_PASSWORD: str = ""
    API_HEALTH_ALERT_SENDER_EMAIL: str = ""
    API_HEALTH_ALERT_RECEIVER_EMAIL: str = ""
    LOCAL_RETENTION_CONNECTION_LOGS_DAYS: int = Field(default=30, ge=1, le=3650)
    LOCAL_RETENTION_AI_ACTIVITY_DAYS: int = Field(default=30, ge=1, le=3650)
    LOCAL_RETENTION_EVALUATION_JOURNAL_DAYS: int = Field(default=3, ge=1, le=3650)
    LOCAL_RETENTION_TRADE_CANDIDATES_DAYS: int = Field(default=3, ge=1, le=3650)
    LOCAL_RETENTION_FEATURE_SNAPSHOTS_DAYS: int = Field(default=3, ge=1, le=3650)
    LOCAL_RETENTION_EXTERNAL_EVENTS_DAYS: int = Field(default=30, ge=1, le=3650)
    LOCAL_RETENTION_EVENT_FETCH_RUNS_DAYS: int = Field(default=30, ge=1, le=3650)

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @field_validator("APP_MODE")
    @classmethod
    def validate_app_mode(cls, value: str) -> str:
        normalized = str(value or "development").strip().lower()
        if normalized not in {"development", "testing", "production"}:
            raise ValueError("APP_MODE must be one of development, testing, production")
        return normalized

    @field_validator("APP_SERVICE_MODE")
    @classmethod
    def validate_app_service_mode(cls, value: str) -> str:
        normalized = str(value or "lite").strip().lower()
        if normalized not in {"lite", "pro", "auto"}:
            raise ValueError("APP_SERVICE_MODE must be one of lite, pro, auto")
        return normalized

    @field_validator("SECRET_STORAGE_MODE")
    @classmethod
    def validate_secret_storage_mode(cls, value: str) -> str:
        normalized = str(value or "keyring").strip().lower()
        if normalized not in {"keyring", "cloud", "encrypted", "insecure_dev"}:
            raise ValueError("SECRET_STORAGE_MODE must be keyring, cloud, encrypted, or insecure_dev")
        return normalized

    @field_validator("EXECUTION_MODE", "AUTO_TRADER_EXECUTION_MODE")
    @classmethod
    def validate_execution_mode(cls, value: str) -> str:
        normalized = str(value or "live").strip().lower()
        if normalized not in {"paper", "shadow", "live"}:
            raise ValueError("execution modes must be paper, shadow, or live")
        return normalized

    @field_validator("API_HOST")
    @classmethod
    def validate_api_host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API_HOST cannot be empty")
        return cleaned

    @field_validator("BENZINGA_API_KEY")
    @classmethod
    def validate_benzinga_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("ACTIVE_ASSET_CLASSES", mode="before")
    @classmethod
    def normalize_asset_classes(cls, value):
        items = _read_csv(value, default=["Forex", "Indices", "Commodities", "Stocks", "Crypto"])
        if not items:
            items = ["Forex", "Indices", "Commodities", "Stocks", "Crypto"]
        normalized = []
        valid = {"Forex", "Indices", "Commodities", "Stocks", "Crypto", "Other"}
        for item in items:
            cleaned = item.strip().title()
            if cleaned not in valid:
                raise ValueError(f"Unsupported asset class '{item}'. Allowed: {sorted(valid)}")
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    @field_validator("ENABLED_SYMBOLS", "AUTO_TRADE_FALLBACK_SYMBOLS", "DISABLED_SYMBOLS", mode="before")
    @classmethod
    def normalize_symbols(cls, value):
        return [item.strip().upper() for item in _read_csv(value) if item.strip()]

    @field_validator("SYMBOL_PROFILE_OVERRIDES", mode="before")
    @classmethod
    def normalize_profile_overrides(cls, value):
        return _read_json_map(value)

    @property
    def api_base_url(self) -> str:
        return f"http://{self.API_HOST}:{self.API_PORT}"

    @property
    def benzinga_available(self) -> bool:
        return self.ENABLE_BENZINGA and bool(self.BENZINGA_API_KEY)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY)

    @property
    def production(self) -> bool:
        return self.APP_MODE == "production"

    @property
    def service_mode_pro_enabled(self) -> bool:
        return self.APP_SERVICE_MODE in {"pro", "auto"}

    @property
    def service_mode_auto_enabled(self) -> bool:
        return self.APP_SERVICE_MODE == "auto"

    @model_validator(mode="after")
    def validate_production_safety(self):
        if not self.production:
            return self
        errors: list[str] = []
        if not self.AUTH_REQUIRED:
            errors.append("AUTH_REQUIRED must be true in production.")
        if not (self.SUPABASE_URL and self.SUPABASE_ANON_KEY and self.SUPABASE_SERVICE_ROLE_KEY):
            errors.append("SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY are required in production.")
        if self.ENABLE_ADMIN_BOOTSTRAP:
            errors.append("ENABLE_ADMIN_BOOTSTRAP must be false in production unless a one-time setup flow is implemented.")
        if (
            self.ADMIN_BOOTSTRAP_USERNAME.strip().lower() == "admin"
            and self.ADMIN_BOOTSTRAP_PASSWORD == "admin"
        ):
            errors.append("Default admin/admin credentials are forbidden in production.")
        if self.SECRET_STORAGE_MODE == "insecure_dev":
            errors.append("SECRET_STORAGE_MODE=insecure_dev is forbidden in production.")
        if self.SECRET_STORAGE_MODE in {"cloud", "encrypted"} and not self.SECRET_ENCRYPTION_KEY:
            errors.append("SECRET_ENCRYPTION_KEY is required when SECRET_STORAGE_MODE is cloud or encrypted in production.")
        if not self.REQUIRE_SECURE_CREDENTIAL_STORAGE:
            errors.append("REQUIRE_SECURE_CREDENTIAL_STORAGE must be true in production.")
        if errors:
            raise ValueError("Unsafe production configuration: " + " ".join(errors))
        return self

    def safe_startup_summary(self) -> dict:
        return {
            "app_mode": self.APP_MODE,
            "service_mode": self.APP_SERVICE_MODE,
            "auth_required": bool(self.AUTH_REQUIRED),
            "live_trading_enabled": bool(self.LIVE_TRADING_ENABLED),
            "auto_trading_enabled_by_default": False,
            "admin_bootstrap_enabled": bool(self.ENABLE_ADMIN_BOOTSTRAP),
            "secret_storage_mode": self.SECRET_STORAGE_MODE,
            "secret_encryption_key_configured": bool(self.SECRET_ENCRYPTION_KEY),
            "execution_mode": self.EXECUTION_MODE,
            "auto_trader_execution_mode": self.AUTO_TRADER_EXECUTION_MODE,
            "supabase_configured": bool(self.supabase_configured),
            "cloud_background_services_enabled": bool(self.service_mode_pro_enabled),
            "auto_background_services_enabled": bool(self.service_mode_auto_enabled),
        }


def load_app_config(env: Mapping[str, str] | None = None) -> AppConfig:
    source = os.environ if env is None else env
    data = {
        "APP_MODE": source.get("APP_MODE", source.get("ENVIRONMENT", "development")).strip().lower(),
        "APP_SERVICE_MODE": source.get("APP_SERVICE_MODE", source.get("SERVICE_MODE", "lite")).strip().lower() or "lite",
        "LIVE_TRADING_ENABLED": _read_bool(source.get("LIVE_TRADING_ENABLED"), False),
        "EXECUTION_MODE": source.get("EXECUTION_MODE", "live").strip().lower() or "live",
        "AUTO_TRADER_EXECUTION_MODE": source.get("AUTO_TRADER_EXECUTION_MODE", "shadow").strip().lower() or "shadow",
        "DEFAULT_TERMINAL_PATH": source.get(
            "MT5_TERMINAL_PATH",
            r"C:\Program Files\MetaTrader 5\terminal64.exe",
        ),
        "DB_PATH": _resolve_db_path(source.get("DB_PATH", "trading_agent.db")),
        "LOG_LEVEL": source.get("LOG_LEVEL", "INFO"),
        "API_HOST": source.get("API_HOST", "127.0.0.1"),
        "API_PORT": int(source.get("API_PORT", "8000")),
        "GEMINI_MODEL": source.get("GEMINI_MODEL", "gemma-4-26b-a4b-it").strip() or "gemma-4-26b-a4b-it",
        "GEMINI_TIMEOUT_SECONDS": float(source.get("GEMINI_TIMEOUT_SECONDS", "12")),
        "GEMINI_MAX_RETRIES": int(source.get("GEMINI_MAX_RETRIES", "1")),
        "SMART_EVAL_PER_SYMBOL_TIMEOUT_SECONDS": float(source.get("SMART_EVAL_PER_SYMBOL_TIMEOUT_SECONDS", "14")),
        "SMART_EVAL_TOTAL_TIMEOUT_SECONDS": float(source.get("SMART_EVAL_TOTAL_TIMEOUT_SECONDS", "60")),
        "SMART_EVAL_MAX_CONCURRENT_EVALS": int(source.get("SMART_EVAL_MAX_CONCURRENT_EVALS", "2")),
        "SMART_EVAL_MAX_SCAN_SYMBOLS_AUTO": int(source.get("SMART_EVAL_MAX_SCAN_SYMBOLS_AUTO", "8")),
        "SMART_EVAL_MAX_SCAN_SYMBOLS_MANUAL": int(source.get("SMART_EVAL_MAX_SCAN_SYMBOLS_MANUAL", "60")),
        "MARKET_TICK_STREAM_INTERVAL_MS": int(source.get("MARKET_TICK_STREAM_INTERVAL_MS", "25")),
        "SAVE_CREDENTIALS_BY_DEFAULT": _read_bool(source.get("SAVE_CREDENTIALS_BY_DEFAULT"), False),
        "REQUIRE_SECURE_CREDENTIAL_STORAGE": _read_bool(source.get("REQUIRE_SECURE_CREDENTIAL_STORAGE"), True),
        "ENABLE_BENZINGA": _read_bool(source.get("ENABLE_BENZINGA"), False),
        "BENZINGA_API_KEY": source.get("BENZINGA_API_KEY", ""),
        "BENZINGA_TIMEOUT_SECONDS": float(source.get("BENZINGA_TIMEOUT_SECONDS", "8")),
        "BENZINGA_MAX_RETRIES": int(source.get("BENZINGA_MAX_RETRIES", "2")),
        "ACTIVE_ASSET_CLASSES": source.get("ACTIVE_ASSET_CLASSES", "Forex,Indices,Commodities,Stocks,Crypto"),
        "ENABLED_SYMBOLS": source.get("ENABLED_SYMBOLS", ""),
        "AUTO_TRADE_FALLBACK_SYMBOLS": source.get(
            "AUTO_TRADE_FALLBACK_SYMBOLS",
            "US500,US100,US30,GER40,AAPL,MSFT,NVDA,AMD,AMZN,TSLA,GOLD,SILVER,WTI,BRENT",
        ),
        "AUTO_TRADE_MAX_SINGLE_TRADE_BUDGET_PCT": float(
            source.get("AUTO_TRADE_MAX_SINGLE_TRADE_BUDGET_PCT", "25")
        ),
        "DISABLED_SYMBOLS": source.get("DISABLED_SYMBOLS", "NATGAS,XNGUSD,NGAS,NG"),
        "SYMBOL_PROFILE_OVERRIDES": source.get(
            "SYMBOL_PROFILE_OVERRIDES",
            json.dumps(
                {
                    "US500": "indices",
                    "US100": "indices",
                    "US30": "indices",
                    "GER40": "indices",
                    "GOLD": "gold",
                    "WTI": "commodities",
                    "BRENT": "commodities",
                }
            ),
        ),
        "ENABLE_RESEARCH_CYCLE": _read_bool(source.get("ENABLE_RESEARCH_CYCLE"), False),
        "AUTO_TRAIN_ON_DEMO": _read_bool(source.get("AUTO_TRAIN_ON_DEMO"), False),
        "AUTO_PROMOTE_ON_DEMO": _read_bool(source.get("AUTO_PROMOTE_ON_DEMO"), False),
        "MIN_TRADES_BEFORE_TRAINING": int(source.get("MIN_TRADES_BEFORE_TRAINING", "50")),
        "TRAINING_WINDOW_DAYS": int(source.get("TRAINING_WINDOW_DAYS", "90")),
        "WALK_FORWARD_WINDOWS": int(source.get("WALK_FORWARD_WINDOWS", "5")),
        "AUTO_META_TRAINING_ENABLED": _read_bool(source.get("AUTO_META_TRAINING_ENABLED"), True),
        "AUTO_META_TRAIN_INTERVAL_SECONDS": int(source.get("AUTO_META_TRAIN_INTERVAL_SECONDS", "900")),
        "AUTO_META_TRAIN_MIN_CLOSED_TRADES": int(source.get("AUTO_META_TRAIN_MIN_CLOSED_TRADES", "20")),
        "AUTO_META_AUTO_APPROVE": _read_bool(source.get("AUTO_META_AUTO_APPROVE"), True),
        "AUTO_META_MIN_PRECISION": float(source.get("AUTO_META_MIN_PRECISION", "0.50")),
        "AUTO_META_MIN_F1": float(source.get("AUTO_META_MIN_F1", "0.45")),
        "AUTO_META_MIN_POSITIVE_SAMPLES": int(source.get("AUTO_META_MIN_POSITIVE_SAMPLES", "12")),
        "AUTO_META_TARGET_COLUMN": source.get("AUTO_META_TARGET_COLUMN", "profitable_after_costs_90m").strip() or "profitable_after_costs_90m",
        "SUPABASE_URL": source.get("SUPABASE_URL", "").strip(),
        "SUPABASE_ANON_KEY": source.get("SUPABASE_ANON_KEY", "").strip(),
        "SUPABASE_SERVICE_ROLE_KEY": source.get("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "AUTH_REQUIRED": _read_bool(source.get("AUTH_REQUIRED"), False),
        "ENABLE_ADMIN_BOOTSTRAP": _read_bool(source.get("ENABLE_ADMIN_BOOTSTRAP"), True),
        "ADMIN_BOOTSTRAP_USERNAME": source.get("ADMIN_BOOTSTRAP_USERNAME", "admin").strip(),
        "ADMIN_BOOTSTRAP_PASSWORD": source.get("ADMIN_BOOTSTRAP_PASSWORD", "admin").strip(),
        "SECRET_STORAGE_MODE": source.get("SECRET_STORAGE_MODE", "keyring").strip().lower() or "keyring",
        "SECRET_ENCRYPTION_KEY": source.get("SECRET_ENCRYPTION_KEY", "").strip(),
        "CLOUD_SYNC_ENABLED": _read_bool(source.get("CLOUD_SYNC_ENABLED"), True),
        "CLOUD_LOG_TABLE": source.get("CLOUD_LOG_TABLE", "runtime_logs").strip() or "runtime_logs",
        "CLOUD_SYNC_TIMEOUT_SECONDS": float(source.get("CLOUD_SYNC_TIMEOUT_SECONDS", "8")),
        "CLOUD_ARCHIVE_ENABLED": _read_bool(source.get("CLOUD_ARCHIVE_ENABLED"), True),
        "CLOUD_ARCHIVE_INTERVAL_SECONDS": int(source.get("CLOUD_ARCHIVE_INTERVAL_SECONDS", "900")),
        "CLOUD_BRAIN_ENABLED": _read_bool(source.get("CLOUD_BRAIN_ENABLED"), True),
        "CLOUD_BRAIN_TABLE": source.get("CLOUD_BRAIN_TABLE", "brain_commands").strip() or "brain_commands",
        "CLOUD_BRAIN_POLL_SECONDS": int(source.get("CLOUD_BRAIN_POLL_SECONDS", "8")),
        "CLOUD_BRAIN_DECISION_ENABLED": _read_bool(source.get("CLOUD_BRAIN_DECISION_ENABLED"), False),
        "CLOUD_BRAIN_DECISION_URL": source.get("CLOUD_BRAIN_DECISION_URL", "").strip(),
        "CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS": float(source.get("CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS", "6")),
        "CLOUD_SECRETS_ENABLED": _read_bool(source.get("CLOUD_SECRETS_ENABLED"), True),
        "CLOUD_SECRETS_TABLE": source.get("CLOUD_SECRETS_TABLE", "app_secrets").strip() or "app_secrets",
        "API_HEALTH_ALERTS_ENABLED": _read_bool(source.get("API_HEALTH_ALERTS_ENABLED"), False),
        "API_HEALTH_ALERT_INTERVAL_SECONDS": int(source.get("API_HEALTH_ALERT_INTERVAL_SECONDS", "60")),
        "API_HEALTH_ALERT_COOLDOWN_SECONDS": int(source.get("API_HEALTH_ALERT_COOLDOWN_SECONDS", "900")),
        "API_HEALTH_SMTP_HOST": source.get("API_HEALTH_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
        "API_HEALTH_SMTP_PORT": int(source.get("API_HEALTH_SMTP_PORT", "587")),
        "API_HEALTH_SMTP_USER": source.get("API_HEALTH_SMTP_USER", "").strip(),
        "API_HEALTH_SMTP_PASSWORD": source.get("API_HEALTH_SMTP_PASSWORD", "").strip(),
        "API_HEALTH_ALERT_SENDER_EMAIL": source.get("API_HEALTH_ALERT_SENDER_EMAIL", "").strip(),
        "API_HEALTH_ALERT_RECEIVER_EMAIL": source.get("API_HEALTH_ALERT_RECEIVER_EMAIL", "").strip(),
        "LOCAL_RETENTION_CONNECTION_LOGS_DAYS": int(source.get("LOCAL_RETENTION_CONNECTION_LOGS_DAYS", "30")),
        "LOCAL_RETENTION_AI_ACTIVITY_DAYS": int(source.get("LOCAL_RETENTION_AI_ACTIVITY_DAYS", "30")),
        "LOCAL_RETENTION_EVALUATION_JOURNAL_DAYS": int(source.get("LOCAL_RETENTION_EVALUATION_JOURNAL_DAYS", "3")),
        "LOCAL_RETENTION_TRADE_CANDIDATES_DAYS": int(source.get("LOCAL_RETENTION_TRADE_CANDIDATES_DAYS", "3")),
        "LOCAL_RETENTION_FEATURE_SNAPSHOTS_DAYS": int(source.get("LOCAL_RETENTION_FEATURE_SNAPSHOTS_DAYS", "3")),
        "LOCAL_RETENTION_EXTERNAL_EVENTS_DAYS": int(source.get("LOCAL_RETENTION_EXTERNAL_EVENTS_DAYS", "30")),
        "LOCAL_RETENTION_EVENT_FETCH_RUNS_DAYS": int(source.get("LOCAL_RETENTION_EVENT_FETCH_RUNS_DAYS", "30")),
    }
    return AppConfig(**data)

try:
    config = load_app_config()
except (ValidationError, ValueError) as exc:  # ValueError covers int/float parsing
    raise RuntimeError(f"Invalid application configuration: {exc}") from exc
