"""
Mean Reversion: Bollinger Bands (20,2) + RSI(14).

BUY signal:
  - Close touches or crosses below lower BB
  - RSI < 35 (normal) or RSI < 30 (strict quality filter)

SELL signal (take profit): Close >= middle BB (SMA20)
Stop loss: ATR-based
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta
from src.strategy.base import BaseStrategy, Signal, StrategySignal


class BollingerRSIStrategy(BaseStrategy):
    def __init__(
        self,
        symbol: str,
        interval: str = "5m",
        bb_length: int = 20,
        bb_std: float = 2.0,
        rsi_length: int = 14,
        rsi_oversold: int = 35,
        rsi_strict: int = 30,  # FR-2.8: quality filter
        atr_length: int = 14,
        risk_multiplier: float = 1.5,
        use_strict_filter: bool = True,  # FR-2.8 toggle
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
        if bb is None:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="BB failed")
        lower_band = bb[f"BBL_{self.bb_length}_{self.bb_std}"]
        mid_band = bb[f"BBM_{self.bb_length}_{self.bb_std}"]

        # RSI
        rsi = ta.rsi(close, length=self.rsi_length)
        if rsi is None:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="RSI failed")

        # ATR
        atr = ta.atr(df["high"], df["low"], close, length=self.atr_length)
        atr_val = (
            atr.iloc[-1]
            if atr is not None and pd.notna(atr.iloc[-1])
            else close.iloc[-1] * 0.01
        )

        current_close = close.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_lower = lower_band.iloc[-1]
        current_mid = mid_band.iloc[-1]

        # Determine RSI threshold (FR-2.8: strict filter for quality)
        rsi_threshold = self.rsi_strict if self.use_strict_filter else self.rsi_oversold

        # BUY signal
        if current_close <= current_lower and current_rsi < rsi_threshold:
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
