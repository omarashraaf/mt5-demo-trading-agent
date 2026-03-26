import MetaTrader5 as mt5
import logging
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ConnectionParams(BaseModel):
    account: int
    password: str
    server: str
    terminal_path: Optional[str] = None


class AccountInfo(BaseModel):
    login: int
    name: str
    server: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int
    currency: str
    trade_mode: int  # 0=demo, 1=contest, 2=real


class TerminalInfo(BaseModel):
    connected: bool
    path: str
    data_path: str
    community_account: bool
    build: int
    name: str


class MT5Connector:
    def __init__(self):
        self._connected = False
        self._account_info: Optional[AccountInfo] = None
        self._last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def account_info(self) -> Optional[AccountInfo]:
        return self._account_info

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def connect(self, params: ConnectionParams) -> bool:
        try:
            # Pass all credentials to initialize() so the terminal
            # can start up and authorize in a single call.
            init_kwargs = {
                "login": params.account,
                "password": params.password,
                "server": params.server,
                "timeout": 15000,  # 15s for terminal startup
            }
            if params.terminal_path:
                init_kwargs["path"] = params.terminal_path

            if not mt5.initialize(**init_kwargs):
                error = mt5.last_error()
                self._last_error = f"MT5 initialize failed: {error}"
                logger.error(self._last_error)
                return False

            # Re-login explicitly in case initialize didn't fully auth
            if not mt5.login(
                login=params.account,
                password=params.password,
                server=params.server,
                timeout=10000,
            ):
                error = mt5.last_error()
                self._last_error = f"MT5 login failed: {error}"
                logger.error(self._last_error)
                mt5.shutdown()
                return False

            info = mt5.account_info()
            if info is None:
                self._last_error = "Failed to retrieve account info after login"
                logger.error(self._last_error)
                mt5.shutdown()
                return False

            self._account_info = AccountInfo(
                login=info.login,
                name=info.name,
                server=info.server,
                balance=info.balance,
                equity=info.equity,
                margin=info.margin,
                free_margin=info.margin_free,
                leverage=info.leverage,
                currency=info.currency,
                trade_mode=info.trade_mode,
            )
            self._connected = True
            self._last_error = None
            logger.info(f"Connected to MT5 account {info.login} on {info.server}")
            return True

        except Exception as e:
            self._last_error = f"Connection exception: {str(e)}"
            logger.exception(self._last_error)
            return False

    def disconnect(self) -> bool:
        try:
            mt5.shutdown()
            self._connected = False
            self._account_info = None
            self._last_error = None
            logger.info("Disconnected from MT5")
            return True
        except Exception as e:
            self._last_error = f"Disconnect exception: {str(e)}"
            logger.exception(self._last_error)
            return False

    def refresh_account(self) -> Optional[AccountInfo]:
        if not self._connected:
            return None
        info = mt5.account_info()
        if info is None:
            self._last_error = f"Failed to refresh account: {mt5.last_error()}"
            return None
        self._account_info = AccountInfo(
            login=info.login,
            name=info.name,
            server=info.server,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            leverage=info.leverage,
            currency=info.currency,
            trade_mode=info.trade_mode,
        )
        return self._account_info

    def get_terminal_info(self) -> Optional[TerminalInfo]:
        if not self._connected:
            return None
        info = mt5.terminal_info()
        if info is None:
            return None
        return TerminalInfo(
            connected=info.connected,
            path=info.path,
            data_path=info.data_path,
            community_account=info.community_account,
            build=info.build,
            name=info.name,
        )

    def verify_terminal(self, path: Optional[str] = None) -> dict:
        try:
            init_kwargs = {}
            if path:
                init_kwargs["path"] = path
            if not mt5.initialize(**init_kwargs):
                error = mt5.last_error()
                return {"ok": False, "error": f"Cannot initialize terminal: {error}"}
            info = mt5.terminal_info()
            result = {
                "ok": True,
                "build": info.build if info else None,
                "name": info.name if info else None,
                "path": info.path if info else None,
            }
            mt5.shutdown()
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def is_demo(self) -> bool:
        if self._account_info is None:
            return False
        return self._account_info.trade_mode == 0
