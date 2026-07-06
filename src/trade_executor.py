"""
Trade Executor: order placement, position tracking. Uses market orders for instant fills.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import src.binance_client as bc
from loguru import logger
from src.config import get_config
from src.database import get_session
from src.models import Order, Position


class TradeExecutor:
    @staticmethod
    def round_qty(quantity: float, step_size: float) -> float:
        if step_size <= 0:
            return quantity
        step = Decimal(str(step_size))
        qty = Decimal(str(quantity))
        return float((qty // step) * step)

    @staticmethod
    def calculate_quantity(balance: float, price: float) -> float:
        cfg = get_config()
        trade_amount = balance * (cfg.bot.trade_amount_pct / 100)
        return trade_amount / price

    def open_position(
        self, symbol: str, price: float, stop_loss: float, take_profit: float
    ) -> bool:
        """Open position using MARKET BUY for instant execution."""
        cfg = get_config()
        session = get_session()

        existing = (
            session.query(Position)
            .filter(Position.symbol == symbol, Position.status == "OPEN")
            .first()
        )
        if existing:
            session.close()
            logger.warning(f"Position already open for {symbol}")
            return False

        balance = bc.get_account_balance("USDT")
        trade_amount = balance * (cfg.bot.trade_amount_pct / 100)

        info = bc.get_symbol_info(symbol)
        if trade_amount < info.get("min_notional", 10):
            session.close()
            logger.warning(
                f"Trade amount {trade_amount:.2f} < min_notional for {symbol}"
            )
            return False

        # Market buy with quoteOrderQty — spend exact USDT
        order = bc.place_market_buy(symbol, trade_amount)
        if not order:
            session.close()
            return False

        fills = order.get("fills", [])
        if not fills:
            session.close()
            logger.error(f"No fills for {symbol} market buy")
            return False

        total_qty = sum(float(f["qty"]) for f in fills)
        total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_cost / total_qty if total_qty > 0 else price

        db_order = Order(
            order_id=str(order.get("orderId", "")),
            client_order_id=order.get("clientOrderId", ""),
            symbol=symbol,
            side="BUY",
            order_type="MARKET",
            price=avg_price,
            orig_qty=total_qty,
            executed_qty=total_qty,
            cummulative_quote_qty=total_cost,
            status=order.get("status", "FILLED"),
            strategy=cfg.bot.strategy,
        )
        session.add(db_order)

        position = Position(
            symbol=symbol,
            side="LONG",
            entry_price=avg_price,
            quantity=total_qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status="OPEN",
            strategy=cfg.bot.strategy,
        )
        session.add(position)
        session.commit()
        session.close()

        logger.info(
            f"Position OPENED: {symbol} qty={total_qty:.6f} entry={avg_price:.2f} SL={stop_loss:.2f} TP={take_profit:.2f}"
        )
        return True

    def close_position(
        self, position: Position, exit_price: float, reason: str = "manual"
    ) -> bool:
        """Close position using MARKET SELL. Checks balance first."""
        session = get_session()
        qty = float(position.quantity)

        # Verify we actually have the asset
        asset = position.symbol.replace("USDT", "")
        actual_balance = bc.get_account_balance(asset)
        if actual_balance < qty:
            logger.error(
                f"Insufficient {asset}: have {actual_balance:.6f}, need {qty:.6f} — marking as closed"
            )
            position.status = "CLOSED"
            position.closed_at = datetime.now(timezone.utc)
            session.commit()
            session.close()
            return False

        order = bc.place_market_sell(position.symbol, qty)
        if not order:
            session.close()
            logger.error(f"Failed to close {position.symbol}")
            return False

        fills = order.get("fills", [])
        total_qty = sum(float(f["qty"]) for f in fills)
        total_revenue = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_exit_price = total_revenue / total_qty if total_qty > 0 else exit_price

        entry = float(position.entry_price)
        pnl = (avg_exit_price - entry) * total_qty

        position.current_price = avg_exit_price
        position.realized_pnl = pnl
        position.status = "CLOSED"
        position.closed_at = datetime.now(timezone.utc)

        db_order = Order(
            order_id=str(order.get("orderId", "")),
            symbol=position.symbol,
            side="SELL",
            order_type="MARKET",
            price=avg_exit_price,
            orig_qty=qty,
            executed_qty=total_qty,
            cummulative_quote_qty=total_revenue,
            status=order.get("status", "FILLED"),
            strategy=position.strategy,
        )
        session.add(db_order)
        session.commit()
        session.close()

        logger.info(
            f"Position CLOSED: {position.symbol} PnL={pnl:.4f} USDT reason={reason}"
        )
        return True

    def emergency_close_all(self) -> int:
        session = get_session()
        positions = session.query(Position).filter(Position.status == "OPEN").all()
        closed = 0
        for pos in positions:
            asset = pos.symbol.replace("USDT", "")
            actual_balance = bc.get_account_balance(asset)
            if actual_balance < float(pos.quantity):
                pos.status = "CLOSED"
                pos.closed_at = datetime.now(timezone.utc)
                closed += 1
                continue
            if bc.place_market_sell(pos.symbol, float(pos.quantity)):
                pos.status = "CLOSED"
                pos.closed_at = datetime.now(timezone.utc)
                closed += 1
        session.commit()
        session.close()
        logger.critical(f"EMERGENCY: closed {closed} positions")
        return closed
