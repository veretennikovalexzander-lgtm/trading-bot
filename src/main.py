"""
Trading Bot — main controller entry point.

Usage:
    python -m src.main start      # Start live trading
    python -m src.main stop       # Stop gracefully
    python -m src.main status     # Show current state
    python -m src.main close BTCUSDT  # Manually close position
    python -m src.main logs 20    # Show recent logs
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from loguru import logger

from src.config import get_config
from src.database import init_db, get_session
from src.models import Position as PositionModel
from src.logger import setup_logging
from src.binance_client import ping, get_current_price, start_websocket
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.risk_manager import RiskManager
from src.trade_executor import TradeExecutor
from src.telegram_bot import create_app, set_controller


class BotController:
    def __init__(self):
        self.running = False
        self.risk_manager = RiskManager()
        self.executor = TradeExecutor()
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.telegram_app = None
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol)

    def get_risk_manager(self) -> RiskManager:
        return self.risk_manager

    def start(self):
        if self.running:
            logger.info("Bot already running")
            return
        self.running = True
        logger.info("=== Trading Bot STARTED ===")
        self._init_websocket()

    def stop(self):
        self.running = False
        logger.info("=== Trading Bot STOPPED ===")

    def manual_close(self, symbol: str) -> bool:
        session = get_session()
        pos = session.query(PositionModel).filter(
            PositionModel.symbol == symbol,
            PositionModel.status == "OPEN",
        ).first()
        if not pos:
            logger.warning(f"No open position for {symbol}")
            return False
        price = get_current_price(symbol)
        return self.executor.close_position(pos, price, reason="manual")

    def show_status(self):
        daily = self.risk_manager.get_daily_stats()
        session = get_session()
        positions = session.query(PositionModel).filter(
            PositionModel.status == "OPEN"
        ).all()

        print(f"\n{'='*40}")
        print(f" Trading Bot Status")
        print(f"{'='*40}")
        print(f" Running: {self.running}")
        print(f" Daily trades: {daily['trades']}")
        print(f" Daily PnL: {daily['pnl']} USDT")
        print(f" Loss streak: {daily['consecutive_losses']}")
        print(f" Breaker: {daily['breaker']}")
        print(f"\n Open positions: {len(positions)}")
        for p in positions:
            print(
                f"  {p.symbol} | entry={float(p.entry_price):.2f} "
                f"| qty={float(p.quantity):.6f} "
                f"| SL={float(p.stop_loss or 0):.2f}"
            )
        print(f"{'='*40}\n")

    def show_logs(self, n: int = 20):
        from src.models import BotLog
        session = get_session()
        logs = (
            session.query(BotLog)
            .order_by(BotLog.created_at.desc())
            .limit(n)
            .all()
        )
        for entry in reversed(logs):
            print(f"[{entry.level}] {entry.message}")

    def _init_websocket(self):
        cfg = get_config()
        try:
            twm = start_websocket(cfg.bot.symbols, self._on_kline)
            twm.join()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")

    def _on_kline(self, msg: dict):
        """Handle incoming kline data from WebSocket."""
        if not self.running:
            return

        data = msg.get("data", {})
        kline = data.get("k", {})
        if not kline or not kline.get("x"):
            return  # Only process closed candles

        symbol = kline.get("s", "")
        if symbol not in self.strategies:
            return

        # Check risk
        allowed, reason = self.risk_manager.is_trading_allowed()
        if not allowed:
            logger.debug(f"Trading blocked: {reason}")
            return

        close_price = float(kline.get("c", 0))
        session = get_session()
        pos = (
            session.query(PositionModel)
            .filter(
                PositionModel.symbol == symbol,
                PositionModel.status == "OPEN",
            )
            .first()
        )

        if pos:
            # Update unrealized PnL
            entry = float(pos.entry_price)
            qty = float(pos.quantity)
            pos.current_price = close_price
            pos.unrealized_pnl = (close_price - entry) * qty
            session.commit()

            # Take profit
            if pos.take_profit and close_price >= float(pos.take_profit):
                self.executor.close_position(pos, close_price, reason="take_profit")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                return

            # Stop loss
            if pos.stop_loss and close_price <= float(pos.stop_loss):
                self.executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                return
        else:
            # Check entry signal
            import pandas as pd

            df = pd.DataFrame([{
                "open": float(kline.get("o", 0)),
                "high": float(kline.get("h", 0)),
                "low": float(kline.get("l", 0)),
                "close": close_price,
                "volume": float(kline.get("v", 0)),
            }])

            # Note: full strategy needs 20+ candles; real version keeps buffer
            signal = self.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                success = self.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if success:
                    logger.info(f"BUY executed for {symbol}")


# --- CLI entry point ---

def main():
    setup_logging()
    init_db()

    if not ping():
        logger.error("Cannot connect to Binance — check API keys and network")
        sys.exit(1)

    controller = BotController()
    set_controller(controller)

    # Start Telegram in background if enabled
    cfg = get_config()
    if cfg.bot.telegram_enabled and cfg.bot.telegram_token:
        controller.telegram_app = create_app()
        if controller.telegram_app:
            import asyncio
            import threading

            def run_telegram():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                controller.telegram_app.run_polling()

            threading.Thread(target=run_telegram, daemon=True).start()
            logger.info("Telegram bot started in background")

    # CLI commands
    if len(sys.argv) >= 2:
        cmd = sys.argv[1].lower()
        if cmd == "start":
            controller.start()
            try:
                while controller.running:
                    time.sleep(1)
            except KeyboardInterrupt:
                controller.stop()
        elif cmd == "stop":
            controller.stop()
        elif cmd == "status":
            controller.show_status()
        elif cmd == "close" and len(sys.argv) >= 3:
            controller.manual_close(sys.argv[2].upper())
        elif cmd == "logs":
            n = int(sys.argv[2]) if len(sys.argv) >= 3 else 20
            controller.show_logs(n)
        else:
            print("Usage: python -m src.main [start|stop|status|close SYMBOL|logs N]")
    else:
        print("Usage: python -m src.main [start|stop|status|close SYMBOL|logs N]")


if __name__ == "__main__":
    main()
