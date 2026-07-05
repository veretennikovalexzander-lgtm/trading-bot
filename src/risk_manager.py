"""
Risk Manager: circuit breaker, drawdown protection, loss streak.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger

from src.config import get_config
from src.database import get_session
from src.models import Trade


class BreakerLevel(Enum):
    NONE = "NONE"
    YELLOW = "YELLOW"    # High volatility → pause 30 min
    ORANGE = "ORANGE"    # -10% drawdown in 24h → stop until manual review
    RED = "RED"          # -5% price drop in 1h → emergency stop


class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.last_trade_time: datetime | None = None
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.day_start_balance = 0.0
        self.breaker_level = BreakerLevel.NONE
        self.breaker_until: datetime | None = None

    def update_pause_if_needed(self, level: BreakerLevel, minutes: int):
        self.breaker_level = level
        self.breaker_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        logger.warning(f"Circuit Breaker: {level.value} — paused for {minutes} min")

    def is_trading_allowed(self) -> tuple[bool, str]:
        """Check all risk conditions; return (allowed, reason)."""
        cfg = get_config()

        # Breaker active?
        if self.breaker_level != BreakerLevel.NONE and self.breaker_until:
            if datetime.now(timezone.utc) < self.breaker_until:
                remaining = (self.breaker_until - datetime.now(timezone.utc)).seconds // 60
                return False, f"Breaker {self.breaker_level.value}: {remaining} min left"
            else:
                self.breaker_level = BreakerLevel.NONE  # Expired

        # Max daily trades?
        if self.daily_trades >= cfg.bot.max_trades_per_day:
            return False, f"Daily trade limit reached ({cfg.bot.max_trades_per_day})"

        # Consecutive losses?
        if self.consecutive_losses >= 3:
            self.update_pause_if_needed(BreakerLevel.YELLOW, 15)
            return False, "3 consecutive losses — pause 15 min"

        return True, "OK"

    def check_price_crash(self, symbol: str, current_price: float, price_1h_ago: float) -> bool:
        """Check for -5% drop in 1 hour → RED breaker."""
        if price_1h_ago > 0:
            change_pct = ((current_price - price_1h_ago) / price_1h_ago) * 100
            if change_pct <= -5:
                self.update_pause_if_needed(BreakerLevel.RED, 60)
                logger.critical(f"RED breaker: {symbol} dropped {change_pct:.1f}% in 1 hour!")
                return True
        return False

    def check_daily_drawdown(self, current_balance: float) -> bool:
        """Check for -10% drawdown in 24h → ORANGE breaker."""
        if self.day_start_balance > 0:
            drawdown = ((current_balance - self.day_start_balance) / self.day_start_balance) * 100
            if drawdown <= -10:
                self.update_pause_if_needed(BreakerLevel.ORANGE, 1440)  # 24h
                logger.critical(f"ORANGE breaker: drawdown {drawdown:.1f}% in 24h")
                return True
        return False

    def record_trade(self, pnl: float):
        self.daily_trades += 1
        self.daily_pnl += pnl
        self.last_trade_time = datetime.now(timezone.utc)
        if pnl <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def get_daily_stats(self) -> dict:
        return {
            "trades": self.daily_trades,
            "pnl": round(self.daily_pnl, 4),
            "consecutive_losses": self.consecutive_losses,
            "breaker": self.breaker_level.value,
        }

    def check_atr_volatility(self, atr_pct: float) -> bool:
        """Check ATR > 3% → YELLOW breaker."""
        if atr_pct > 3.0:
            self.update_pause_if_needed(BreakerLevel.YELLOW, 30)
            logger.warning(f"YELLOW breaker: ATR {atr_pct:.1f}% > 3%")
            return True
        return False
