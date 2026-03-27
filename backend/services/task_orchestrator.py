from __future__ import annotations


class TaskOrchestrator:
    def __init__(self, auto_trader):
        self.auto_trader = auto_trader

    def start_auto_trade(self):
        self.auto_trader.start()

    def stop_auto_trade(self):
        self.auto_trader.stop()

    def status_snapshot(self) -> dict:
        return {
            "auto_trader": self.auto_trader.status_snapshot(),
            "position_manager": self.auto_trader.position_manager.status_snapshot(),
        }
