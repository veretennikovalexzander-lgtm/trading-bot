"""
Mean Reversion: Bollinger Bands + RSI.
"""
from __future__ import annotations
import time as _time
from collections import defaultdict
import pandas as pd
import pandas_ta as ta
from src.strategy.base import BaseStrategy, Signal, StrategySignal

_cooldowns: dict[str, float] = defaultdict(float)
COOLDOWN_SEC = 300


def _get_bb_columns(df_bb):
    cols = list(df_bb.columns)
    lower = [c for c in cols if c.startswith("BBL_")][0]
    mid = [c for c in cols if c.startswith("BBM_")][0]
    upper = [c for c in cols if c.startswith("BBU_")][0]
    return lower, mid, upper


class BollingerRSIStrategy(BaseStrategy):
    def __init__(self, symbol: str, interval: str = "1m", bb_length: int = 20,
                 bb_std: float = 2.0, rsi_length: int = 14, rsi_oversold: int = 40,
                 rsi_strict: int = 30, atr_length: int = 14, risk_multiplier: float = 1.5,
                 use_strict_filter: bool = False, debug: bool = False):
        super().__init__(symbol, interval)
        self.bb_length = bb_length; self.bb_std = bb_std
        self.rsi_length = rsi_length; self.rsi_oversold = rsi_oversold
        self.rsi_strict = rsi_strict; self.atr_length = atr_length
        self.risk_multiplier = risk_multiplier
        self.use_strict_filter = use_strict_filter
        self.debug = debug

    def analyze(self, df: pd.DataFrame) -> StrategySignal:
        if len(df) < self.bb_length + 5:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="Not enough data")
        close = df["close"]
        bb = ta.bbands(close, length=self.bb_length, std=self.bb_std)
        if bb is None or bb.empty:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="BB failed")
        lower_col, mid_col, upper_col = _get_bb_columns(bb)
        lower = bb[lower_col]; mid = bb[mid_col]
        rsi = ta.rsi(close, length=self.rsi_length)
        if rsi is None or rsi.empty:
            return StrategySignal(Signal.HOLD, self.symbol, 0, reason="RSI failed")
        atr = ta.atr(df["high"], df["low"], close, length=self.atr_length)
        atr_val = float(atr.iloc[-1]) if atr is not None and len(atr) > 0 and pd.notna(atr.iloc[-1]) else float(close.iloc[-1]) * 0.01
        cc = float(close.iloc[-1]); cr = float(rsi.iloc[-1])
        cl = float(lower.iloc[-1]); cm = float(mid.iloc[-1])
        thresh = self.rsi_strict if self.use_strict_filter else self.rsi_oversold

        if self.debug:
            from loguru import logger
            logger.info(f"{self.symbol} price={cc:.2f} lowerBB={cl:.2f} midBB={cm:.2f} RSI={cr:.1f} thresh={thresh}")

        if cc <= cl and cr < thresh:
            now = _time.time()
            if now - _cooldowns[self.symbol] < COOLDOWN_SEC:
                return StrategySignal(Signal.HOLD, self.symbol, cc, reason="Cooldown")
            _cooldowns[self.symbol] = now
            sl = cc - (self.risk_multiplier * atr_val)
            return StrategySignal(Signal.BUY, self.symbol, cc, stop_loss=sl, take_profit=cm,
                                  reason=f"BB cross + RSI={cr:.1f}")
        return StrategySignal(Signal.HOLD, self.symbol, cc, reason="No signal")
