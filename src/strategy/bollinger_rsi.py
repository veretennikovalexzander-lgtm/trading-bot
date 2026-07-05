"""
Mean Reversion: Bollinger Bands (20,2) + RSI(14).
"""

from __future__ import annotations

import time as _time
from collections import defaultdict

import pandas as pd
import pandas_ta as ta
from src.strategy.base import BaseStrategy, Signal, StrategySignal

_cooldowns: dict[str, float] = defaultdict(float)
COOLDOWN_SEC = 300


def _get_bb_columns(df_bb: pd.DataFrame) -> tuple[str, str, str]:
    """Find BB column names regardless of pandas-ta version."""
    cols = list(df_bb.columns)
    lower = [c for c in cols if c.startswith("BBL_")][0]
    mid = [c for c in cols if c.startswith("BBM_")][0]
    upper = [c for c in cols if c.startswith("BBU_")][0]
    return lower, mid, upper


class BollingerRSIStrategy(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        interval: str = "1m",
        bb_length: int = 20,
        bb_std: float = 2.0,
        rsi_length: int = 14,
        rsi_oversold: int = 35,
        rsi_strict: int = 30,
        atr_length: int = 14,
        risk_multiplier: float = 1.5,
        use_strict_filter: bool = False,
    ):
        super().__init__(symbol, interval)
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.rsi_length = rsi_length
        self.rsi_oversold = rsi_oversold
        self.rsi_strict = rsi_strict
        self.atr_length = atr_length
        self.risk_multiplier = risk_multiplier
        self.use_strict_filter = use_strict_filter

    def analyze(self, df: pd.DataFrame) -> StrategySignal:
        if len(df) < self.bb_length + 5:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="Not enough data")

        close = df["close"]

        # Bollinger Bands
        bb = ta.bbands(close, length=self.bb_length, std=self.bb_std)
        if bb is None or bb.empty:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="BB failed")
        lower_col, mid_col, upper_col = _get_bb_columns(bb)
        lower_band = bb[lower_col]
        mid_band = bb[mid_col]

        # RSI
        rsi = ta.rsi(close, length=self.rsi_length)
        if rsi is None or rsi.empty:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="RSI failed")

        # ATR
        atr = ta.atr(df["high"], df["low"], close, length=self.atr_length)
        if atr is not None and len(atr) > 0 and pd.notna(atr.iloc[-1]):
            atr_val = float(atr.iloc[-1])
        else:
            atr_val = float(close.iloc[-1]) * 0.01

        current_close = float(close.iloc[-1])
        current_rsi = float(rsi.iloc[-1])
        current_lower = float(lower_band.iloc[-1])
        current_mid = float(mid_band.iloc[-1])

        rsi_threshold = self.rsi_strict if self.use_strict_filter else self.rsi_oversold

        # BUY signal
        if current_close <= current_lower and current_rsi < rsi_threshold:
            now = _time.time()
            if now - _cooldowns[self.symbol] < COOLDOWN_SEC:
                return StrategySignal(
                    Signal.HOLD, self.symbol, current_close, reason="Cooldown active"
                )

            _cooldowns[self.symbol] = now

            stop_loss = current_close - (self.risk_multiplier * atr_val)
            quality = "STRICT" if self.use_strict_filter else "NORMAL"
            return StrategySignal(
                signal=Signal.BUY,
                symbol=self.symbol,
                price=current_close,
                stop_loss=stop_loss,
                take_profit=current_mid,
                reason=f"BB cross below + RSI={current_rsi:.1f} [{quality}]",
            )

        return StrategySignal(
            Signal.HOLD, self.symbol, current_close, reason="No signal"
        )
