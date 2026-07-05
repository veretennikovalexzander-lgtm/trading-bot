"""
Mean Reversion strategy: Bollinger Bands (20,2) + RSI(14).

BUY signal:
  - Close touches or crosses below lower BB
  - RSI < 35 (oversold)
  
SELL signal (take profit):
  - Close >= middle BB (SMA20)

Stop loss:
  - ATR-based, risk 1-5% from entry
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
        atr_length: int = 14,
        risk_multiplier: float = 1.5,
    ):
        super().__init__(symbol, interval)
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.rsi_length = rsi_length
        self.rsi_oversold = rsi_oversold
        self.atr_length = atr_length
        self.risk_multiplier = risk_multiplier

    def analyze(self, df: pd.DataFrame) -> StrategySignal:
        if len(df) < self.bb_length + 5:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="Not enough data")

        close = df["close"]

        # --- Bollinger Bands ---
        bb = ta.bbands(close, length=self.bb_length, std=self.bb_std)
        if bb is None:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="BB calculation failed")

        lower_band = bb[f"BBL_{self.bb_length}_{self.bb_std}"]
        mid_band = bb[f"BBM_{self.bb_length}_{self.bb_std}"]

        # --- RSI ---
        rsi = ta.rsi(close, length=self.rsi_length)
        if rsi is None:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="RSI calculation failed")

        # --- ATR for stop loss ---
        atr = ta.atr(df["high"], df["low"], close, length=self.atr_length)
        if atr is None:
            atr_val = close.iloc[-1] * 0.01  # fallback 1%
        else:
            atr_val = atr.iloc[-1] if pd.notna(atr.iloc[-1]) else close.iloc[-1] * 0.01

        current_close = close.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_lower = lower_band.iloc[-1]
        current_mid = mid_band.iloc[-1]
        prev_close = close.iloc[-2]
        prev_lower = lower_band.iloc[-2]

        # --- BUY signal: cross below lower band + RSI oversold ---
        if current_close <= current_lower and current_rsi < self.rsi_oversold:
            stop_loss = current_close - (self.risk_multiplier * atr_val)
            take_profit = current_mid
            return StrategySignal(
                signal=Signal.BUY,
                symbol=self.symbol,
                price=current_close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=f"BB cross below + RSI={current_rsi:.1f}",
            )

        return StrategySignal(Signal.HOLD, self.symbol, current_close, reason="No signal")
