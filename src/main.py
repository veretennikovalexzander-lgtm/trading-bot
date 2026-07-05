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
import threading
import time
from datetime import datetime, timedelta, timezone

from loguru import logger
from src.binance_client import get_current_price, ping, start_websocket
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import Position as PositionModel
from src.risk_manager import BreakerLevel, RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.telegram_bot import create_app, send_notification, set_controller
from src.trade_executor import TradeExecutor

# Network watchdog constants
PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10


class NetworkWatchdog:
    """Monitors network health: ping failures + WebSocket disconnects."""

    def __init__(self, controller: "BotController"):
        self.controller = controller
        self.ping_failures = 0
        self.ws_disconnects: list[datetime] = []
        self._watchdog_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self):
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(target=self._run, daemon=True)
        self._watchdog_thread.start()
        logger.info("Network watchdog started")

    def stop(self):
        self._stop_event.set()
        logger.info("Network watchdog stopped")

    def _run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(PING_INTERVAL_SEC)
            if self._stop_event.is_set():
                break

            if ping():
                self.ping_failures = 0
            else:
                self.ping_failures += 1
                logger.warning(
                    f"Ping failed ({self.ping_failures}/{MAX_PING_FAILURES})"
                )

            if self.ping_failures >= MAX_PING_FAILURES:
                logger.critical(
                    f"NETWORK LOST: {MAX_PING_FAILURES} ping failures — stopping bot"
                )
                self.controller._notify_or_log(
                    "\U0001f534 *КРИТИЧЕСКАЯ ОШИБКА*: потеряно соединение с сетью. Бот остановлен."
                )
                self.controller.stop()
                return

            self._check_ws_reconnects()

    def record_ws_disconnect(self):
        now = datetime.now(timezone.utc)
        self.ws_disconnects.append(now)
        cutoff = now - timedelta(minutes=WS_RECONNECT_WINDOW_MIN)
        self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]

        recent = len(self.ws_disconnects)
        if recent >= MAX_WS_RECONNECTS:
            logger.critical(
                f"NETWORK UNSTABLE: {recent} WebSocket disconnects "
                f"in {WS_RECONNECT_WINDOW_MIN} min — stopping bot"
            )
            self.controller._notify_or_log(
                "\U0001f534 *КРИТИЧЕСКАЯ ОШИБКА*: частые разрывы соединения с биржей. Бот остановлен."
            )
            self.controller.stop()

    def _check_ws_reconnects(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=WS_RECONNECT_WINDOW_MIN)
        self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]


class BotController:
    def __init__(self):
        self.running = False
        self.risk_manager = RiskManager()
        self.executor = TradeExecutor()
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.telegram_app = None
        self.watchdog = NetworkWatchdog(self)
        self._ws_twm = None
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol)

    def get_risk_manager(self) -> RiskManager:
        return self.risk_manager

    def _notify_or_log(self, text: str):
        """Send Telegram notification or log."""
        if self.telegram_app:
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(send_notification(self.telegram_app, text))
            except Exception:
                pass
        logger.info(text)

    def start(self):
        if self.running:
            logger.info("Bot already running")
            return

        if not ping():
            logger.error("Cannot connect to Binance — check API keys and network")
            return

        self.running = True
        self.watchdog.start()
        logger.info("=== Trading Bot STARTED ===")
        self._init_websocket()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.watchdog.stop()
        if self._ws_twm:
            try:
                self._ws_twm.stop()
            except Exception:
                pass
            self._ws_twm = None
        logger.info("=== Trading Bot STOPPED ===")

    def manual_close(self, symbol: str) -> bool:
        session = get_session()
        pos = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )
        if not pos:
            logger.warning(f"No open position for {symbol}")
            return False
        price = get_current_price(symbol)
        return self.executor.close_position(pos, price, reason="manual")

    def show_status(self):
        daily = self.risk_manager.get_daily_stats()
        session = get_session()
        positions = (
            session.query(PositionModel).filter(PositionModel.status == "OPEN").all()
        )

        ping_ok = ping()
        ws_disconnects = len(self.watchdog.ws_disconnects)

        print(f"\n{'=' * 40}")
        print(f" Trading Bot Status")
        print(f"{'=' * 40}")
        print(f" Running: {self.running}")
        print(f" Network: {'OK' if ping_ok else 'FAIL'}")
        print(f" WS disconnects (10 min): {ws_disconnects}")
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
        print(f"{'=' * 40}\n")

    def show_logs(self, n: int = 20):
        from src.models import BotLog

        session = get_session()
        logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        for entry in reversed(logs):
            print(f"[{entry.level}] {entry.message}")

    def _init_websocket(self):
        cfg = get_config()
        try:
            from binance import ThreadedWebsocketManager

            self._ws_twm = ThreadedWebsocketManager(
                api_key=cfg.binance.api_key,
                api_secret=cfg.binance.api_secret,
                testnet=cfg.binance.testnet,
            )
            self._ws_twm.start()
            streams = [f"{s.lower()}@kline_5m" for s in cfg.bot.symbols]
            self._ws_twm.start_multiplex_socket(
                callback=self._on_kline, streams=streams
            )
            logger.info(f"WebSocket started for: {cfg.bot.symbols}")
            self._ws_twm.join()
        except Exception as e:
            logger.error(f"WebSocket startup error: {e}")
            self.watchdog.record_ws_disconnect()

    def _on_kline(self, msg: dict):
        if not self.running:
            return

        data = msg.get("data", {})
        kline = data.get("k", {})
        if not kline or not kline.get("x"):
            return

        symbol = kline.get("s", "")
        if symbol not in self.strategies:
            return

        allowed, reason = self.risk_manager.is_trading_allowed()
        if not allowed:
            return

        close_price = float(kline.get("c", 0))
        session = get_session()
        pos = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )

        if pos:
            entry = float(pos.entry_price)
            qty = float(pos.quantity)
            pos.current_price = close_price
            pos.unrealized_pnl = (close_price - entry) * qty
            session.commit()

            if pos.take_profit and close_price >= float(pos.take_profit):
                self.executor.close_position(pos, close_price, reason="take_profit")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                return

            if pos.stop_loss and close_price <= float(pos.stop_loss):
                self.executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                return
        else:
            import pandas as pd

            df = pd.DataFrame(
                [
                    {
                        "open": float(kline.get("o", 0)),
                        "high": float(kline.get("h", 0)),
                        "low": float(kline.get("l", 0)),
                        "close": close_price,
                        "volume": float(kline.get("v", 0)),
                    }
                ]
            )

            signal = self.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                success = self.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if success:
                    self._notify_or_log(
                        f"\U0001f7e2 *BUY* {symbol} @ {signal.price:.2f} "
                        f"| SL: {signal.stop_loss:.2f} | TP: {signal.take_profit:.2f}"
                    )


def main():
    setup_logging()
    init_db()

    if not ping():
        logger.error("Cannot connect to Binance — check API keys and network")
        sys.exit(1)
    logger.info("Network check passed")

    controller = BotController()
    set_controller(controller)

    cfg = get_config()
    if cfg.bot.telegram_enabled and cfg.bot.telegram_token:
        controller.telegram_app = create_app()
        if controller.telegram_app:
            import asyncio

            def run_telegram():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                controller.telegram_app.run_polling()

            threading.Thread(target=run_telegram, daemon=True).start()
            logger.info("Telegram bot started in background")

    if len(sys.argv) >= 2:
        cmd = sys.argv[1].lower()
        if cmd == "start":
            controller.start()
            try:
                while controller.running:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt — stopping bot")
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
