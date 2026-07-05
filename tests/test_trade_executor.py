"""
Unit tests for TradeExecutor.
"""

from __future__ import annotations

import pytest
from src.trade_executor import TradeExecutor


class TestRoundQty:
    def test_round_down(self):
        result = TradeExecutor.round_qty(0.123456, 0.01)
        assert result == 0.12

    def test_round_exact(self):
        result = TradeExecutor.round_qty(0.15, 0.01)
        assert result == 0.15

    def test_round_zero_step(self):
        result = TradeExecutor.round_qty(0.123, 0)
        assert result == 0.123

    def test_round_tiny_step(self):
        result = TradeExecutor.round_qty(0.12345678, 0.000001)
        assert result == 0.123456

    def test_round_eth_like(self):
        result = TradeExecutor.round_qty(1.234567, 0.0001)
        assert result == 1.2345


class TestCalculateQuantity:
    def test_basic_calculation(self, monkeypatch):
        monkeypatch.setenv("BOT_TRADE_AMOUNT_PCT", "10")
        import src.config as cfg
        from src.config import _config

        cfg._config = None
        qty = TradeExecutor.calculate_quantity(100.0, 62000.0)
        expected = 10.0 / 62000.0
        assert abs(qty - expected) < 0.00000001

    def test_50_percent(self, monkeypatch):
        monkeypatch.setenv("BOT_TRADE_AMOUNT_PCT", "50")
        import src.config as cfg

        cfg._config = None
        qty = TradeExecutor.calculate_quantity(100.0, 100.0)
        assert qty == 0.5
