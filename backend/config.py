from __future__ import annotations

import json
import os
from collections.abc import Mapping

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

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

    If DB_PATH is relative and the file is missing in current cwd, try one-level
    parent (legacy layout where DB sat at repo root while backend cwd is /backend).
    """
    cleaned = (value or "trading_agent.db").strip().strip('"').strip("'")
    if not cleaned:
        cleaned = "trading_agent.db"
    if os.path.isabs(cleaned):
        return cleaned

    primary = os.path.abspath(cleaned)
    if os.path.exists(primary):
        return primary

    parent_candidate = os.path.abspath(os.path.join(os.getcwd(), "..", cleaned))
    if os.path.exists(parent_candidate):
        return parent_candidate

    return primary


class AppConfig(BaseModel):
    LIVE_TRADING_ENABLED: bool = False
    DEFAULT_TERMINAL_PATH: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    DB_PATH: str = "trading_agent.db"
    LOG_LEVEL: str = "INFO"
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    GEMINI_TIMEOUT_SECONDS: float = Field(default=12.0, gt=1.0, le=60.0)
    GEMINI_MAX_RETRIES: int = Field(default=1, ge=0, le=3)
    SAVE_CREDENTIALS_BY_DEFAULT: bool = False
    REQUIRE_SECURE_CREDENTIAL_STORAGE: bool = True
    ENABLE_FINNHUB: bool = False
    FINNHUB_API_KEY: str = ""
    FINNHUB_TIMEOUT_SECONDS: float = Field(default=8.0, gt=1.0, le=30.0)
    FINNHUB_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    ACTIVE_ASSET_CLASSES: list[str] = Field(default_factory=lambda: ["Indices", "Commodities", "Stocks"])
    ENABLED_SYMBOLS: list[str] = Field(default_factory=list)
    AUTO_TRADE_FALLBACK_SYMBOLS: list[str] = Field(
        default_factory=lambda: [
            "US500",
            "US100",
            "US30",
            "GER40",
            "GOLD",
            "WTI",
            "BRENT",
            "AAPL",
            "MSFT",
            "NVDA",
            "AMD",
            "AMZN",
            "TSLA",
        ]
    )
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
    AUTO_META_TRAIN_MIN_CLOSED_TRADES: int = Field(default=30, ge=10, le=100000)
    AUTO_META_AUTO_APPROVE: bool = True
    AUTO_META_MIN_PRECISION: float = Field(default=0.50, ge=0.0, le=1.0)
    AUTO_META_MIN_F1: float = Field(default=0.45, ge=0.0, le=1.0)
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    AUTH_REQUIRED: bool = False
    ENABLE_ADMIN_BOOTSTRAP: bool = True
    ADMIN_BOOTSTRAP_USERNAME: str = "admin"
    ADMIN_BOOTSTRAP_PASSWORD: str = "admin"
    CLOUD_SYNC_ENABLED: bool = True
    CLOUD_LOG_TABLE: str = "runtime_logs"
    CLOUD_SYNC_TIMEOUT_SECONDS: float = Field(default=8.0, gt=1.0, le=30.0)
    CLOUD_BRAIN_ENABLED: bool = True
    CLOUD_BRAIN_TABLE: str = "brain_commands"
    CLOUD_BRAIN_POLL_SECONDS: int = Field(default=8, ge=3, le=300)
    CLOUD_BRAIN_DECISION_ENABLED: bool = False
    CLOUD_BRAIN_DECISION_URL: str = ""
    CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS: float = Field(default=6.0, gt=1.0, le=30.0)

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @field_validator("API_HOST")
    @classmethod
    def validate_api_host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API_HOST cannot be empty")
        return cleaned

    @field_validator("FINNHUB_API_KEY")
    @classmethod
    def validate_finnhub_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("ACTIVE_ASSET_CLASSES", mode="before")
    @classmethod
    def normalize_asset_classes(cls, value):
        items = _read_csv(value, default=["Indices", "Commodities", "Stocks"])
        if not items:
            items = ["Indices", "Commodities", "Stocks"]
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
    def finnhub_available(self) -> bool:
        return self.ENABLE_FINNHUB and bool(self.FINNHUB_API_KEY)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY)


def load_app_config(env: Mapping[str, str] | None = None) -> AppConfig:
    source = env or os.environ
    data = {
        "LIVE_TRADING_ENABLED": _read_bool(source.get("LIVE_TRADING_ENABLED"), False),
        "DEFAULT_TERMINAL_PATH": source.get(
            "MT5_TERMINAL_PATH",
            r"C:\Program Files\MetaTrader 5\terminal64.exe",
        ),
        "DB_PATH": _resolve_db_path(source.get("DB_PATH", "trading_agent.db")),
        "LOG_LEVEL": source.get("LOG_LEVEL", "INFO"),
        "API_HOST": source.get("API_HOST", "127.0.0.1"),
        "API_PORT": int(source.get("API_PORT", "8000")),
        "GEMINI_TIMEOUT_SECONDS": float(source.get("GEMINI_TIMEOUT_SECONDS", "12")),
        "GEMINI_MAX_RETRIES": int(source.get("GEMINI_MAX_RETRIES", "1")),
        "SAVE_CREDENTIALS_BY_DEFAULT": _read_bool(source.get("SAVE_CREDENTIALS_BY_DEFAULT"), False),
        "REQUIRE_SECURE_CREDENTIAL_STORAGE": _read_bool(source.get("REQUIRE_SECURE_CREDENTIAL_STORAGE"), True),
        "ENABLE_FINNHUB": _read_bool(source.get("ENABLE_FINNHUB"), False),
        "FINNHUB_API_KEY": source.get("FINNHUB_API_KEY", ""),
        "FINNHUB_TIMEOUT_SECONDS": float(source.get("FINNHUB_TIMEOUT_SECONDS", "8")),
        "FINNHUB_MAX_RETRIES": int(source.get("FINNHUB_MAX_RETRIES", "2")),
        "ACTIVE_ASSET_CLASSES": source.get("ACTIVE_ASSET_CLASSES", "Indices,Commodities,Stocks"),
        "ENABLED_SYMBOLS": source.get("ENABLED_SYMBOLS", ""),
        "AUTO_TRADE_FALLBACK_SYMBOLS": source.get(
            "AUTO_TRADE_FALLBACK_SYMBOLS",
            "US500,US100,US30,GER40,GOLD,WTI,BRENT,AAPL,MSFT,NVDA,AMD,AMZN,TSLA",
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
        "AUTO_META_TRAIN_MIN_CLOSED_TRADES": int(source.get("AUTO_META_TRAIN_MIN_CLOSED_TRADES", "30")),
        "AUTO_META_AUTO_APPROVE": _read_bool(source.get("AUTO_META_AUTO_APPROVE"), True),
        "AUTO_META_MIN_PRECISION": float(source.get("AUTO_META_MIN_PRECISION", "0.50")),
        "AUTO_META_MIN_F1": float(source.get("AUTO_META_MIN_F1", "0.45")),
        "SUPABASE_URL": source.get("SUPABASE_URL", "").strip(),
        "SUPABASE_ANON_KEY": source.get("SUPABASE_ANON_KEY", "").strip(),
        "SUPABASE_SERVICE_ROLE_KEY": source.get("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "AUTH_REQUIRED": _read_bool(source.get("AUTH_REQUIRED"), False),
        "ENABLE_ADMIN_BOOTSTRAP": _read_bool(source.get("ENABLE_ADMIN_BOOTSTRAP"), True),
        "ADMIN_BOOTSTRAP_USERNAME": source.get("ADMIN_BOOTSTRAP_USERNAME", "admin").strip(),
        "ADMIN_BOOTSTRAP_PASSWORD": source.get("ADMIN_BOOTSTRAP_PASSWORD", "admin").strip(),
        "CLOUD_SYNC_ENABLED": _read_bool(source.get("CLOUD_SYNC_ENABLED"), True),
        "CLOUD_LOG_TABLE": source.get("CLOUD_LOG_TABLE", "runtime_logs").strip() or "runtime_logs",
        "CLOUD_SYNC_TIMEOUT_SECONDS": float(source.get("CLOUD_SYNC_TIMEOUT_SECONDS", "8")),
        "CLOUD_BRAIN_ENABLED": _read_bool(source.get("CLOUD_BRAIN_ENABLED"), True),
        "CLOUD_BRAIN_TABLE": source.get("CLOUD_BRAIN_TABLE", "brain_commands").strip() or "brain_commands",
        "CLOUD_BRAIN_POLL_SECONDS": int(source.get("CLOUD_BRAIN_POLL_SECONDS", "8")),
        "CLOUD_BRAIN_DECISION_ENABLED": _read_bool(source.get("CLOUD_BRAIN_DECISION_ENABLED"), False),
        "CLOUD_BRAIN_DECISION_URL": source.get("CLOUD_BRAIN_DECISION_URL", "").strip(),
        "CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS": float(source.get("CLOUD_BRAIN_DECISION_TIMEOUT_SECONDS", "6")),
    }
    return AppConfig(**data)


try:
    config = load_app_config()
except (ValidationError, ValueError) as exc:  # ValueError covers int/float parsing
    raise RuntimeError(f"Invalid application configuration: {exc}") from exc
