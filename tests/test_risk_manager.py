"""Unit tests for RiskManager."""
from __future__ import annotations
import pytest
from src.risk_manager import BreakerLevel

@pytest.fixture
def rm(fresh_rm):
    return fresh_rm

class TestRiskManager:
    def test_initial_state(self, rm):
        assert rm.consecutive_losses == 0
        assert rm.daily_trades == 0
    def test_trading_allowed(self, rm):
        ok, reason = rm.is_trading_allowed()
        assert ok is True
    def test_record_trades(self, rm):
        rm.record_trade(10); rm.record_trade(-5)
        assert rm.daily_trades == 2
        assert rm.daily_pnl == 5.0
        assert rm.consecutive_losses == 1
    def test_three_losses_pause(self, rm):
        rm.record_trade(-1); rm.record_trade(-1); rm.record_trade(-1)
        ok, reason = rm.is_trading_allowed()
        assert ok is False
    def test_max_daily(self, rm):
        for _ in range(30): rm.record_trade(1)
        assert rm.is_trading_allowed()[0] is False
    def test_price_crash(self, rm):
        assert rm.check_price_crash("BTC", 58900, 62000) is True
    def test_no_crash(self, rm):
        assert rm.check_price_crash("BTC", 61500, 62000) is False
    def test_drawdown(self, rm):
        rm.day_start_balance = 10000
        assert rm.check_daily_drawdown(8900) is True
    def test_drawdown_ok(self, rm):
        rm.day_start_balance = 10000
        assert rm.check_daily_drawdown(9500) is False
    def test_atr_volatile(self, rm):
        assert rm.check_atr_volatility(5.0) is True
    def test_atr_calm(self, rm):
        assert rm.check_atr_volatility(1.0) is False
