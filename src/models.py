"""
SQLAlchemy ORM models for all trading bot tables.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from src.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class TradingPair(Base):
    __tablename__ = "trading_pairs"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False)
    base_asset = Column(String(10), nullable=False)
    quote_asset = Column(String(10), nullable=False)
    min_qty = Column(Numeric(18, 8), default=0)
    step_size = Column(Numeric(18, 8), default=0)
    tick_size = Column(Numeric(18, 8), default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class BotConfig(Base):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String(64), unique=True)
    client_order_id = Column(String(64))
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY / SELL
    order_type = Column(String(20), nullable=False)  # LIMIT / MARKET / ...
    price = Column(Numeric(18, 8))
    stop_price = Column(Numeric(18, 8))
    orig_qty = Column(Numeric(18, 8), nullable=False)
    executed_qty = Column(Numeric(18, 8), default=0)
    cummulative_quote_qty = Column(Numeric(18, 8), default=0)
    status = Column(String(20), nullable=False, default="NEW")
    time_in_force = Column(String(10), default="GTC")
    strategy = Column(String(50))
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    trade_id = Column(String(64), unique=True)
    order_id = Column(String(64), nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    price = Column(Numeric(18, 8), nullable=False)
    qty = Column(Numeric(18, 8), nullable=False)
    quote_qty = Column(Numeric(18, 8), nullable=False)
    commission = Column(Numeric(18, 8), default=0)
    commission_asset = Column(String(10), default="USDT")
    realized_pnl = Column(Numeric(18, 8), default=0)
    is_buyer = Column(Boolean, default=False)
    is_maker = Column(Boolean, default=False)
    strategy = Column(String(50))
    trade_time = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)  # LONG / SHORT
    entry_price = Column(Numeric(18, 8), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=False)
    current_price = Column(Numeric(18, 8))
    unrealized_pnl = Column(Numeric(18, 8), default=0)
    realized_pnl = Column(Numeric(18, 8), default=0)
    stop_loss = Column(Numeric(18, 8))
    take_profit = Column(Numeric(18, 8))
    status = Column(String(20), nullable=False, default="OPEN")
    strategy = Column(String(50))
    opened_at = Column(DateTime(timezone=True), default=_utcnow)
    closed_at = Column(DateTime(timezone=True))


class MarketData(Base):
    __tablename__ = "market_data"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    interval = Column(String(10), nullable=False)
    open_time = Column(DateTime(timezone=True), nullable=False)
    close_time = Column(DateTime(timezone=True), nullable=False)
    open = Column(Numeric(18, 8), nullable=False)
    high = Column(Numeric(18, 8), nullable=False)
    low = Column(Numeric(18, 8), nullable=False)
    close = Column(Numeric(18, 8), nullable=False)
    volume = Column(Numeric(18, 8), nullable=False)
    quote_volume = Column(Numeric(18, 8))
    trades_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "open_time", name="uq_market_data"),
    )


class BotLog(Base):
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True)
    level = Column(String(10), nullable=False, default="INFO")
    category = Column(String(50))
    message = Column(Text, nullable=False)
    metadata_ = Column("metadata", JSONB)
    created_at = Column(DateTime(timezone=True), default=_utcnow, index=True)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(Integer, primary_key=True)
    total_balance = Column(Numeric(18, 8), nullable=False)
    available_balance = Column(Numeric(18, 8), nullable=False)
    locked_balance = Column(Numeric(18, 8), default=0)
    balances_json = Column(JSONB)
    snapshot_time = Column(DateTime(timezone=True), default=_utcnow)
