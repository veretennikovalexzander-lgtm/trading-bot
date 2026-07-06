"""BotController — coordinates all subsystems. No I/O logic."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from src.config import get_config
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"


class BotController:
    """Main coordinator: holds state, delegates to subsystems."""

    def __init__(self):
        self.running = False
        self.started_at: datetime | None = None
        self.trades_count = 0
        self.risk_manager = RiskManager()
        self.executor = TradeExecutor()
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.candles: dict[str, deque[dict]] = {}
        self.last_close_times: dict[str, int] = {}

        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.candles[symbol] = deque(maxlen=100)
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol, interval="1m")

    def write_status(self, extra: dict | None = None):
        data = {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "trades": self.trades_count,
            **(extra or {}),
            **(self.risk_manager.get_daily_stats()),
        }
        STATUS_FILE.write_text(json.dumps(data, indent=2))
