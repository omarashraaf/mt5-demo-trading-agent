from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Optional


class AgentInput(BaseModel):
    symbol: str
    timeframe: str
    bars: list[dict]  # List of OHLCV dicts
    spread: float
    account_equity: float
    open_positions: list[dict]
    multi_tf_bars: Optional[dict[str, list[dict]]] = None  # {"M15": [...], "H1": [...], "H4": [...]}


class TradeSignal(BaseModel):
    action: str  # BUY, SELL, or HOLD
    confidence: float  # 0.0 to 1.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    max_holding_minutes: Optional[int] = None
    reason: str = ""
    strategy: Optional[str] = None  # "trend_follow", "mean_reversion", etc.


class TradingAgent(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    def evaluate(self, input_data: AgentInput) -> TradeSignal:
        pass
