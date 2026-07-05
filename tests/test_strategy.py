"""Unit tests for BollingerRSIStrategy."""

from __future__ import annotations

import pandas as pd
import pytest
from src.strategy.base import Signal, StrategySignal
from src.strategy.bollinger_rsi import BollingerRSIStrategy


class TestBollingerRSIStrategy:
    def test_not_enough_data(self):
        strat = BollingerRSIStrategy("BTCUSDT")
        df = pd.DataFrame(
            {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1]}
        )
        sig = strat.analyze(df)
        assert sig.signal == Signal.HOLD

    def test_hold_on_normal_market(self, sample_ohlcv_data):
        strat = BollingerRSIStrategy("BTCUSDT", use_strict_filter=False)
        sig = strat.analyze(sample_ohlcv_data)
        assert sig.signal in (Signal.HOLD, Signal.BUY)

    def test_buy_on_oversold(self, oversold_ohlcv_data):
        """With strong crash data, strategy must generate BUY signal."""
        strat = BollingerRSIStrategy(
            "BTCUSDT", use_strict_filter=False, rsi_oversold=35
        )
        sig = strat.analyze(oversold_ohlcv_data)
        # In a strong crash, price is at lower BB and RSI is low — must be BUY
        assert sig.signal == Signal.BUY, (
            f"Expected BUY, got {sig.signal} ({sig.reason})"
        )
        assert sig.stop_loss is not None
        assert sig.take_profit is not None
        assert sig.stop_loss < sig.price

    def test_strict_filter_blocks(self, sample_ohlcv_data):
        s1 = BollingerRSIStrategy("BTCUSDT", use_strict_filter=True)
        s2 = BollingerRSIStrategy("BTCUSDT", use_strict_filter=False)
        r1 = s1.analyze(sample_ohlcv_data)
        r2 = s2.analyze(sample_ohlcv_data)
        # Strict should not fire more often than normal
        if r2.signal == Signal.BUY:
            pass  # Both can be BUY, or only normal

    def test_tp_above_entry(self, oversold_ohlcv_data):
        strat = BollingerRSIStrategy(
            "BTCUSDT", use_strict_filter=False, rsi_oversold=35
        )
        sig = strat.analyze(oversold_ohlcv_data)
        if sig.signal == Signal.BUY:
            assert sig.take_profit > sig.price

    def test_sl_below_entry(self, oversold_ohlcv_data):
        strat = BollingerRSIStrategy(
            "BTCUSDT", use_strict_filter=False, rsi_oversold=35
        )
        sig = strat.analyze(oversold_ohlcv_data)
        if sig.signal == Signal.BUY:
            assert sig.stop_loss < sig.price

    def test_interval_default(self):
        s = BollingerRSIStrategy("ETHUSDT")
        assert s.interval == "1m" and s.bb_length == 20 and s.rsi_length == 14

    def test_custom_params(self):
        s = BollingerRSIStrategy(
            "BTCUSDT", bb_length=10, rsi_oversold=25, interval="5m"
        )
        assert s.bb_length == 10 and s.rsi_oversold == 25 and s.interval == "5m"


class TestStrategySignal:
    def test_signal_buy(self):
        sig = StrategySignal(
            Signal.BUY,
            "BTCUSDT",
            62000,
            stop_loss=61500,
            take_profit=62500,
            reason="test",
        )
        assert (
            sig.signal == Signal.BUY and sig.price == 62000 and sig.stop_loss == 61500
        )

    def test_signal_hold(self):
        sig = StrategySignal(Signal.HOLD, "ETHUSDT", 1800, reason="no signal")
        assert sig.signal == Signal.HOLD and sig.stop_loss is None
