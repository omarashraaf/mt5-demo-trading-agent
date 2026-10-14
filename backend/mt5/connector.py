import logging
import time
from typing import Optional
from pydantic import BaseModel
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except Exception:  # pragma: no cover - handled at runtime for non-Windows/cloud runtimes
    class _MT5Stub:
        def __getattr__(self, name):
            if name.isupper():
                return 0
            return lambda *args, **kwargs: None

    mt5 = _MT5Stub()
    MT5_AVAILABLE = False

logger = logging.getLogger(__name__)


class ConnectionParams(BaseModel):
    account: int
    password: str
    server: str
    terminal_path: Optional[str] = None


class TerminalSessionParams(BaseModel):
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
    trade_allowed: bool = False
    tradeapi_disabled: bool = False


class MT5Connector:
    def __init__(self):
        self._connected = False
        self._account_info: Optional[AccountInfo] = None
        self._last_error: Optional[str] = None
        self._last_params: Optional[ConnectionParams] = None
        self._last_recover_at: float = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def account_info(self) -> Optional[AccountInfo]:
        return self._account_info

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _is_auth_error(self, error: object) -> bool:
        text = str(error or "").lower()
        if not text:
            return False
        return any(
            token in text
            for token in (
                "authorization failed",
                "invalid account",
                "invalid password",
                "invalid server",
                "auth",
                "-6",
            )
        )

    def _init_terminal(self, params: ConnectionParams, *, with_credentials: bool) -> bool:
        init_kwargs = {
            "timeout": 15000 if with_credentials else 10000,
        }
        if params.terminal_path:
            init_kwargs["path"] = params.terminal_path
        if with_credentials:
            init_kwargs["login"] = int(params.account)
            init_kwargs["password"] = params.password
            init_kwargs["server"] = params.server
        if mt5.initialize(**init_kwargs):
            return True
        # Some MT5 installations reject explicit path intermittently; retry once
        # without path so the bridge can attach to the currently running terminal.
        if params.terminal_path and "path" in init_kwargs:
            retry_kwargs = dict(init_kwargs)
            retry_kwargs.pop("path", None)
            return bool(mt5.initialize(**retry_kwargs))
        return False

    def connect(self, params: ConnectionParams) -> bool:
        if not MT5_AVAILABLE:
            self._last_error = "MetaTrader5 Python package is unavailable in this runtime."
            logger.error(self._last_error)
            return False
        try:
            started_at = time.perf_counter()
            # Reset any stale IPC session first. MT5 Python bridge can remain
            # half-open across refreshes, which makes the next initialize/login
            # look like a slow hang instead of a clean reconnect.
            try:
                mt5.shutdown()
            except Exception:
                pass

            if not self._init_terminal(params, with_credentials=False):
                init_error = mt5.last_error()
                # Authorization failures can happen at initialize stage depending on
                # terminal/account state. Retry with credentialed initialize.
                if self._is_auth_error(init_error):
                    logger.warning(
                        "MT5 initialize authorization issue detected, retrying with login credentials. error=%s",
                        init_error,
                    )
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                    if not self._init_terminal(params, with_credentials=True):
                        error = mt5.last_error()
                        self._last_error = (
                            f"MT5 initialize failed: {error}. Verify MT5 account password (not investor password) "
                            f"and exact server name '{params.server}'."
                        )
                        logger.error(self._last_error)
                        return False
                else:
                    self._last_error = f"MT5 initialize failed: {init_error}"
                    logger.error(self._last_error)
                    return False

            if not mt5.login(
                login=params.account,
                password=params.password,
                server=params.server,
                timeout=15000,
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
            self._last_params = params.model_copy()
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "Connected to MT5 account %s on %s in %sms",
                info.login,
                info.server,
                elapsed_ms,
            )
            return True

        except Exception as e:
            self._last_error = f"Connection exception: {str(e)}"
            logger.exception(self._last_error)
            return False

    def connect_terminal_session(self, params: TerminalSessionParams) -> bool:
        """Attach to a local MT5 terminal session without receiving credentials."""
        if not MT5_AVAILABLE:
            self._last_error = "MetaTrader5 Python package is unavailable in this runtime."
            logger.error(self._last_error)
            return False
        try:
            started_at = time.perf_counter()
            try:
                mt5.shutdown()
            except Exception:
                pass

            init_kwargs = {"timeout": 15000}
            if params.terminal_path:
                init_kwargs["path"] = params.terminal_path
            if not mt5.initialize(**init_kwargs):
                error = mt5.last_error()
                self._last_error = (
                    f"MT5 terminal session attach failed: {error}. Open MetaTrader 5 locally, "
                    "log in inside the terminal, then click Connect again."
                )
                logger.error(self._last_error)
                return False

            info = mt5.account_info()
            terminal = mt5.terminal_info()
            if info is None:
                self._last_error = (
                    "MT5 terminal is open but no logged-in trading account is available. "
                    "Log in inside MetaTrader 5, then click Connect again."
                )
                logger.error(self._last_error)
                self._connected = False
                return False
            if terminal is not None and not bool(getattr(terminal, "connected", False)):
                self._last_error = "MT5 terminal is not connected to the trade server."
                logger.error(self._last_error)
                self._connected = False
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
            self._last_params = None
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "Attached to local MT5 terminal account %s on %s in %sms",
                info.login,
                info.server,
                elapsed_ms,
            )
            return True
        except Exception as e:
            self._last_error = f"Terminal session connect exception: {str(e)}"
            logger.exception(self._last_error)
            return False

    def disconnect(self) -> bool:
        if not MT5_AVAILABLE:
            self._connected = False
            self._account_info = None
            return True
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
        if not MT5_AVAILABLE:
            self._last_error = "MetaTrader5 Python package is unavailable in this runtime."
            return None
        if not self._connected:
            return None
        terminal = self.get_terminal_info()
        if terminal is None or not terminal.connected:
            self._last_error = "MT5 terminal is not connected to the trade server"
            if self._recover_connection(self._last_error):
                terminal = self.get_terminal_info()
            if terminal is None or not terminal.connected:
                self._connected = False
                return None
        info = mt5.account_info()
        if info is None:
            err = mt5.last_error()
            self._last_error = f"Failed to refresh account: {err}"
            if self._recover_connection(err):
                info = mt5.account_info()
            if info is None:
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

    def _is_ipc_error(self, error: object) -> bool:
        text = str(error or "").lower()
        if not text:
            return False
        return any(
            token in text
            for token in (
                "ipc send failed",
                "no ipc connection",
                "not initialized",
                "terminal is not connected",
            )
        )

    def _recover_connection(self, error: object) -> bool:
        if not MT5_AVAILABLE or not self._is_ipc_error(error):
            return False
        now = time.time()
        if (now - float(self._last_recover_at or 0.0)) < 2.0:
            return False
        self._last_recover_at = now
        params = self._last_params
        if params is None:
            try:
                mt5.shutdown()
            except Exception:
                pass
            try:
                ok = bool(mt5.initialize(timeout=7000))
                if ok:
                    logger.warning("Recovered MT5 session via initialize after IPC error: %s", error)
                    self._connected = True
                    self._last_error = None
                return ok
            except Exception:
                return False
        logger.warning("Attempting MT5 reconnect after IPC error: %s", error)
        ok = self.connect(params)
        if not ok:
            logger.error("MT5 reconnect failed after IPC error.")
        return ok

    def get_terminal_info(self) -> Optional[TerminalInfo]:
        if not MT5_AVAILABLE:
            return None
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
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            tradeapi_disabled=bool(getattr(info, "tradeapi_disabled", False)),
        )

    def trade_server_connected(self) -> bool:
        terminal = self.get_terminal_info()
        return bool(terminal and terminal.connected)

    def verify_terminal(self, path: Optional[str] = None) -> dict:
        if not MT5_AVAILABLE:
            return {"ok": False, "error": "MetaTrader5 Python package is unavailable in this runtime."}
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
