"""
Unit tests for SQLAlchemy ORM models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from src.models import (
    AccountSnapshot,
    BotConfig,
    BotLog,
    MarketData,
    Order,
    Position,
    Trade,
    TradingPair,
)


class TestTradingPair:
    def test_create(self, in_memory_db):
        pair = TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
        in_memory_db.add(pair)
        in_memory_db.commit()
        result = in_memory_db.query(TradingPair).first()
        assert result.symbol == "BTCUSDT"
        assert result.base_asset == "BTC"

    def test_unique_symbol(self, in_memory_db):
        in_memory_db.add(
            TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
        )
        in_memory_db.commit()
        with pytest.raises(Exception):
            in_memory_db.add(
                TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
            )
            in_memory_db.commit()


class TestBotConfig:
    def test_create_and_read(self, in_memory_db):
        cfg = BotConfig(
            config_key="risk_per_trade", config_value="3", description="Risk %"
        )
        in_memory_db.add(cfg)
        in_memory_db.commit()
        result = in_memory_db.query(BotConfig).first()
        assert result.config_key == "risk_per_trade"
        assert result.config_value == "3"


class TestOrder:
    def test_create_order(self, in_memory_db):
        order = Order(
            order_id="12345",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            price=62000,
            orig_qty=0.01,
            status="FILLED",
        )
        in_memory_db.add(order)
        in_memory_db.commit()
        result = in_memory_db.query(Order).first()
        assert result.order_id == "12345"
        assert result.side == "BUY"


class TestPosition:
    def test_create_position(self, in_memory_db):
        pos = Position(
            symbol="BTCUSDT",
            side="LONG",
            entry_price=62000,
            quantity=0.01,
            stop_loss=61500,
            take_profit=62500,
            status="OPEN",
        )
        in_memory_db.add(pos)
        in_memory_db.commit()
        result = in_memory_db.query(Position).first()
        assert result.status == "OPEN"
        assert float(result.entry_price) == 62000.0

    def test_close_position(self, in_memory_db):
        pos = Position(
            symbol="BTCUSDT",
            side="LONG",
            entry_price=62000,
            quantity=0.01,
            status="OPEN",
        )
        in_memory_db.add(pos)
        in_memory_db.commit()
        pos.status = "CLOSED"
        pos.realized_pnl = 50.0
        pos.closed_at = datetime.now(timezone.utc)
        in_memory_db.commit()
        result = in_memory_db.query(Position).first()
        assert result.status == "CLOSED"


class TestMarketData:
    def test_create_candle(self, in_memory_db):
        now = datetime.now(timezone.utc)
        md = MarketData(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now,
            close_time=now,
            open=62000,
            high=62100,
            low=61900,
            close=62050,
            volume=10,
        )
        in_memory_db.add(md)
        in_memory_db.commit()
        result = in_memory_db.query(MarketData).first()
        assert result.symbol == "BTCUSDT"
        assert result.interval == "1m"


class TestBotLog:
    def test_create_log(self, in_memory_db):
        log = BotLog(level="INFO", category="test", message="Test message")
        in_memory_db.add(log)
        in_memory_db.commit()
        result = in_memory_db.query(BotLog).first()
        assert result.level == "INFO"
        assert result.category == "test"


class TestAccountSnapshot:
    def test_create_snapshot(self, in_memory_db):
        snap = AccountSnapshot(
            total_balance=10000,
            available_balance=9500,
            locked_balance=500,
            balances_json={"BTC": 0.01, "USDT": 9500},
        )
        in_memory_db.add(snap)
        in_memory_db.commit()
        result = in_memory_db.query(AccountSnapshot).first()
        assert float(result.total_balance) == 10000
        assert result.balances_json["BTC"] == 0.01
