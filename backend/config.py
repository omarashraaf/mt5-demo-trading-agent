from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()


class AppConfig(BaseModel):
    # SAFETY: Live trading is disabled by default. Enable at your own risk.
    # Demo trading only is the default and recommended mode.
    LIVE_TRADING_ENABLED: bool = False
    DEFAULT_TERMINAL_PATH: str = os.getenv(
        "MT5_TERMINAL_PATH",
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
    )
    DB_PATH: str = os.getenv("DB_PATH", "trading_agent.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))


config = AppConfig()
