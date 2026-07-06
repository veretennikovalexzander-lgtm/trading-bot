"""Trade Executor: MARKET BUY + OCO SELL (TP + SL on exchange)."""

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

    def open_position(
        self, symbol: str, price: float, stop_loss: float, take_profit: float
    ) -> bool:
        """MARKET BUY, then OCO SELL (TP + SL) on exchange."""
        cfg = get_config()
        session = get_session()

        existing = (
            session.query(Position)
            .filter(Position.symbol == symbol, Position.status == "OPEN")
            .first()
        )
        if existing:
            session.close()
            logger.warning(f"Position exists: {symbol}")
            return False

        balance = bc.get_account_balance("USDT")
        trade_amount = balance * (cfg.bot.trade_amount_pct / 100)

        info = bc.get_symbol_info(symbol)
        if trade_amount < info.get("min_notional", 10):
            session.close()
            logger.warning(f"Amount {trade_amount:.2f} < min")
            return False

        # Step 1: MARKET BUY
        order = bc.place_market_buy(symbol, trade_amount)
        if not order:
            session.close()
            return False

        fills = order.get("fills", [])
        if not fills:
            session.close()
            logger.error(f"No fills: {symbol}")
            return False

        total_qty = sum(float(f["qty"]) for f in fills)
        total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_cost / total_qty if total_qty > 0 else price

        # Step 2: OCO SELL (stop-loss + take-profit on exchange!)
        sl_order = bc.place_oco_sell(symbol, total_qty, take_profit, stop_loss)

        # Save buy order
        db_order = Order(
            order_id=str(order.get("orderId", "")),
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

        # Create position record
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
            f"POSITION: {symbol} qty={total_qty:.6f} @ {avg_price:.2f} SL={stop_loss:.2f} TP={take_profit:.2f} OCO={'OK' if sl_order else 'FAIL'}"
        )
        return True

    def close_position(
        self, position: Position, exit_price: float, reason: str = "manual"
    ) -> bool:
        """Close position: check balance, then MARKET SELL."""
        session = get_session()
        qty = float(position.quantity)
        asset = position.symbol.replace("USDT", "")
        actual = bc.get_account_balance(asset)
        if actual < qty:
            logger.error(
                f"Insufficient {asset}: have {actual:.6f} need {qty:.6f} — marking closed"
            )
            position.status = "CLOSED"
            position.closed_at = datetime.now(timezone.utc)
            session.commit()
            session.close()
            return False

        order = bc.place_market_sell(position.symbol, qty)
        if not order:
            session.close()
            logger.error(f"Close failed: {position.symbol}")
            return False

        fills = order.get("fills", [])
        total_qty = sum(float(f["qty"]) for f in fills)
        total_rev = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_exit = total_rev / total_qty if total_qty > 0 else exit_price
        pnl = (avg_exit - float(position.entry_price)) * total_qty

        position.current_price = avg_exit
        position.realized_pnl = pnl
        position.status = "CLOSED"
        position.closed_at = datetime.now(timezone.utc)

        db_order = Order(
            order_id=str(order.get("orderId", "")),
            symbol=position.symbol,
            side="SELL",
            order_type="MARKET",
            price=avg_exit,
            orig_qty=qty,
            executed_qty=total_qty,
            cummulative_quote_qty=total_rev,
            status=order.get("status", "FILLED"),
            strategy=position.strategy,
        )
        session.add(db_order)
        session.commit()
        session.close()
        logger.info(f"CLOSED: {position.symbol} PnL={pnl:.4f} reason={reason}")
        return True

    def emergency_close_all(self) -> int:
        session = get_session()
        positions = session.query(Position).filter(Position.status == "OPEN").all()
        closed = 0
        for pos in positions:
            asset = pos.symbol.replace("USDT", "")
            if bc.get_account_balance(asset) < float(pos.quantity):
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
