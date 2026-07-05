"""
Trade Executor: order placement, position tracking, profit fixation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger

import src.binance_client as bc
from src.config import get_config
from src.database import get_session
from src.models import Order, Position, Trade as TradeModel


class TradeExecutor:
    @staticmethod
    def round_qty(quantity: float, step_size: float) -> float:
        """Round quantity to symbol step size."""
        if step_size == 0:
            return quantity
        precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
        step = Decimal(str(step_size))
        qty = Decimal(str(quantity))
        rounded = (qty // step) * step
        return float(rounded)

    @staticmethod
    def calculate_quantity(balance: float, price: float) -> float:
        """Calculate trade quantity: 10% of balance / price."""
        cfg = get_config()
        trade_amount = balance * (cfg.bot.trade_amount_pct / 100)
        qty = trade_amount / price
        return qty

    def open_position(self, symbol: str, price: float, stop_loss: float, take_profit: float) -> bool:
        """Open a new long position."""
        cfg = get_config()
        session = get_session()

        # Check existing position
        existing = session.query(Position).filter(
            Position.symbol == symbol,
            Position.status == "OPEN",
        ).first()
        if existing:
            logger.warning(f"Position already open for {symbol}")
            return False

        balance = bc.get_account_balance("USDT")
        qty = self.calculate_quantity(balance, price)

        info = bc.get_symbol_info(symbol)
        qty = self.round_qty(qty, info["step_size"])

        if qty < info["min_qty"]:
            logger.warning(f"Qty {qty} < min {info['min_qty']} for {symbol}")
            return False

        order = bc.place_limit_buy(symbol, qty, price)
        if not order:
            return False

        # Save order
        db_order = Order(
            order_id=str(order.get("orderId", "")),
            client_order_id=order.get("clientOrderId", ""),
            symbol=symbol,
            side="BUY",
            order_type="LIMIT",
            price=price,
            orig_qty=qty,
            status=order.get("status", "NEW"),
            strategy=cfg.bot.strategy,
        )
        session.add(db_order)

        # Create position
        position = Position(
            symbol=symbol,
            side="LONG",
            entry_price=price,
            quantity=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status="OPEN",
            strategy=cfg.bot.strategy,
        )
        session.add(position)
        session.commit()

        logger.info(f"Position OPENED: {symbol} qty={qty} entry={price} SL={stop_loss} TP={take_profit}")
        return True

    def close_position(self, position: Position, exit_price: float, reason: str = "manual") -> bool:
        """Close a position, calculate PnL."""
        session = get_session()

        order = bc.place_limit_sell(position.symbol, float(position.quantity), exit_price)
        if not order:
            logger.error(f"Failed to close position for {position.symbol}")
            return False

        # PnL
        entry = float(position.entry_price)
        qty = float(position.quantity)
        pnl = (exit_price - entry) * qty

        # Update position
        position.current_price = exit_price
        position.realized_pnl = pnl
        position.status = "CLOSED"
        position.closed_at = datetime.now(timezone.utc)

        # Save sell order
        db_order = Order(
            order_id=str(order.get("orderId", "")),
            symbol=position.symbol,
            side="SELL",
            order_type="LIMIT",
            price=exit_price,
            orig_qty=qty,
            status=order.get("status", "NEW"),
            strategy=position.strategy,
        )
        session.add(db_order)
        session.commit()

        logger.info(f"Position CLOSED: {position.symbol} PnL={pnl:.4f} USDT reason={reason}")
        return True

    def emergency_close_all(self) -> int:
        """Emergency market sell all open positions."""
        session = get_session()
        positions = session.query(Position).filter(Position.status == "OPEN").all()
        closed = 0
        for pos in positions:
            if bc.place_market_sell(pos.symbol, float(pos.quantity)):
                pos.status = "CLOSED"
                pos.closed_at = datetime.now(timezone.utc)
                closed += 1
        session.commit()
        logger.critical(f"EMERGENCY: closed {closed} positions")
        return closed
