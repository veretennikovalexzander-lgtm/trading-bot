"""
Risk Manager: circuit breaker, drawdown protection, loss streak.
Counters persist via JSON file to survive restarts (FR-3.6).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from loguru import logger
from src.config import get_config

STATE_FILE = Path(__file__).resolve().parents[1] / "risk_state.json"


class BreakerLevel(Enum):
    NONE = "NONE"
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"


class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.day_start_balance = 0.0
        self._last_date: str = ""  # ISO date for daily reset
        self.breaker_level = BreakerLevel.NONE
        self.breaker_until: datetime | None = None
        self._load_state()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.consecutive_losses = data.get("consecutive_losses", 0)
                self.daily_trades = data.get("daily_trades", 0)
                self.daily_pnl = data.get("daily_pnl", 0.0)
                self._last_date = data.get("last_date", "")
                logger.debug(f"Risk state loaded: {self.daily_trades} trades today")
            except Exception:
                pass

    def _save_state(self):
        STATE_FILE.write_text(
            json.dumps(
                {
                    "consecutive_losses": self.consecutive_losses,
                    "daily_trades": self.daily_trades,
                    "daily_pnl": round(self.daily_pnl, 4),
                    "last_date": datetime.now(timezone.utc).date().isoformat(),
                },
                indent=2,
            )
        )

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._last_date:
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self._last_date = today
            logger.info("Daily stats reset")

    def update_pause_if_needed(self, level: BreakerLevel, minutes: int):
        self.breaker_level = level
        self.breaker_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        logger.warning(f"Circuit Breaker: {level.value} — {minutes} min")

    def is_trading_allowed(self) -> tuple[bool, str]:
        self._check_daily_reset()
        cfg = get_config()

        if self.breaker_level != BreakerLevel.NONE and self.breaker_until:
            if datetime.now(timezone.utc) < self.breaker_until:
                remaining = int(
                    (self.breaker_until - datetime.now(timezone.utc)).total_seconds()
                    // 60
                )
                return (
                    False,
                    f"Breaker {self.breaker_level.value}: {remaining} min left",
                )
            else:
                self.breaker_level = BreakerLevel.NONE

        if self.daily_trades >= cfg.bot.max_trades_per_day:
            return False, f"Daily trade limit ({cfg.bot.max_trades_per_day})"

        if self.consecutive_losses >= 3:
            self.update_pause_if_needed(BreakerLevel.YELLOW, 15)
            return False, "3 consecutive losses — pause 15 min"

        return True, "OK"

    def check_price_crash(
        self, symbol: str, current: float, price_1h_ago: float
    ) -> bool:
        if price_1h_ago > 0:
            change = ((current - price_1h_ago) / price_1h_ago) * 100
            if change <= -5:
                self.update_pause_if_needed(BreakerLevel.RED, 60)
                logger.critical(f"RED breaker: {symbol} {change:.1f}% in 1 hour")
                return True
        return False

    def check_daily_drawdown(self, current_balance: float) -> bool:
        if self.day_start_balance > 0:
            dd = (
                (current_balance - self.day_start_balance) / self.day_start_balance
            ) * 100
            if dd <= -10:
                self.update_pause_if_needed(BreakerLevel.ORANGE, 1440)
                logger.critical(f"ORANGE breaker: drawdown {dd:.1f}%")
                return True
        return False

    def record_trade(self, pnl: float):
        self._check_daily_reset()
        self.daily_trades += 1
        self.daily_pnl += pnl
        if pnl <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self._save_state()

    def get_daily_stats(self) -> dict:
        return {
            "trades": self.daily_trades,
            "pnl": round(self.daily_pnl, 4),
            "consecutive_losses": self.consecutive_losses,
            "breaker": self.breaker_level.value,
        }

    def check_atr_volatility(self, atr_pct: float) -> bool:
        if atr_pct > 3.0:
            self.update_pause_if_needed(BreakerLevel.YELLOW, 30)
            logger.warning(f"YELLOW breaker: ATR {atr_pct:.1f}%")
            return True
        return False
