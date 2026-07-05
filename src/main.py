"""
Trading Bot — main controller (CLI-only version).

Usage:
    python -m src.main start         Start live trading
    python -m src.main stop          Stop gracefully
    python -m src.main status        Show current state
    python -m src.main close BTCUSDT Close position manually
    python -m src.main logs 20       Show recent logs
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from src.binance_client import get_current_price, ping
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import Position as PositionModel
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

# Network watchdog
PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10

# Status file for inter-process communication
STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"


class NetworkWatchdog:
    """Monitors ping failures and WebSocket reconnect storms."""

    def __init__(self, controller: "BotController"):
        self.controller = controller
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.ping_failures = 0
        self.ws_disconnects: list[datetime] = []

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Network watchdog started")

    def stop(self):
        self._stop_event.set()

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
                logger.critical("NETWORK LOST — stopping bot")
                self.controller.stop()
                return
            self._prune_disconnects()

    def record_ws_disconnect(self):
        now = datetime.now(timezone.utc)
        self.ws_disconnects.append(now)
        self._prune_disconnects()
        if len(self.ws_disconnects) >= MAX_WS_RECONNECTS:
            logger.critical(f"NETWORK UNSTABLE — stopping bot")
            self.controller.stop()

    def _prune_disconnects(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=WS_RECONNECT_WINDOW_MIN)
        self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]


class BotController:
    def __init__(self):
        self.running = False
        self.risk_manager = RiskManager()
        self.executor = TradeExecutor()
        self.watchdog = NetworkWatchdog(self)
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self._ws_twm = None
        self._started_at: datetime | None = None
        self._total_trades = 0
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol)

    def _write_status(self, extra: dict | None = None):
        """Write current status to JSON file for external status checks."""
        data = {
            "running": self.running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "network_ok": ping(),
            "ws_disconnects_10m": len(self.watchdog.ws_disconnects),
            "total_trades": self._total_trades,
            **(self.risk_manager.get_daily_stats()),
            **(extra or {}),
        }
        STATUS_FILE.write_text(json.dumps(data, indent=2))

    def _delete_status(self):
        if STATUS_FILE.exists():
            STATUS_FILE.unlink()

    def start(self):
        if self.running:
            print("Bot already running")
            return
        if not ping():
            print("ERROR: Cannot connect to Binance")
            return
        self.running = True
        self._started_at = datetime.now(timezone.utc)
        self.watchdog.start()
        self._write_status()
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
        self._write_status({"stopped_at": datetime.now(timezone.utc).isoformat()})
        logger.info("=== Trading Bot STOPPED ===")

    def manual_close(self, symbol: str) -> bool:
        session = get_session()
        pos = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )
        if not pos:
            print(f"No open position for {symbol}")
            return False
        price = get_current_price(symbol)
        return self.executor.close_position(pos, price, reason="manual")

    def show_status(self):
        daily = self.risk_manager.get_daily_stats()
        session = get_session()
        positions = (
            session.query(PositionModel).filter(PositionModel.status == "OPEN").all()
        )

        # Try to read running status from file
        running = False
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                running = data.get("running", False)
            except Exception:
                pass

        ping_ok = ping()
        ws_dc = len(self.watchdog.ws_disconnects)

        print(f"\n{'=' * 40}")
        print(" Trading Bot Status")
        print(f"{'=' * 40}")
        print(f" Running: {running}")
        print(f" Network: {'OK' if ping_ok else 'FAIL'} | WS DC (10m): {ws_dc}")
        print(f" Daily trades: {daily['trades']} | PnL: {daily['pnl']} USDT")
        print(
            f" Loss streak: {daily['consecutive_losses']} | Breaker: {daily['breaker']}"
        )
        if positions:
            print(f"\n Open positions ({len(positions)}):")
            for p in positions:
                print(
                    f"  {p.symbol} | entry={float(p.entry_price):.2f} | qty={float(p.quantity):.6f} | SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n Open positions: 0")
        print(f"{'=' * 40}\n")

    def show_logs(self, n: int = 20):
        from src.models import BotLog

        session = get_session()
        logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        for e in reversed(logs):
            print(f"[{e.level}] {e.message}")

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
            logger.info(f"WebSocket: {cfg.bot.symbols}")
            self._ws_twm.join()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
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

        allowed, _ = self.risk_manager.is_trading_allowed()
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
            qty = float(pos.quantity)
            pos.current_price = close_price
            pos.unrealized_pnl = (close_price - float(pos.entry_price)) * qty
            session.commit()
            self._write_status({"last_price": {symbol: close_price}})

            if pos.take_profit and close_price >= float(pos.take_profit):
                self.executor.close_position(pos, close_price, reason="take_profit")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                self._total_trades += 1
                logger.info(f"TP {symbol} PnL={pos.unrealized_pnl:.2f}")
                return
            if pos.stop_loss and close_price <= float(pos.stop_loss):
                self.executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                self._total_trades += 1
                logger.info(f"SL {symbol} PnL={pos.unrealized_pnl:.2f}")
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
                ok = self.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if ok:
                    self._total_trades += 1
                    logger.info(
                        f"BUY {symbol} @ {signal.price:.2f} SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f}"
                    )
            self._write_status({"last_price": {symbol: close_price}})


def main():
    setup_logging()
    init_db()

    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""

    # Status / close / logs don't need a live controller
    if cmd == "status":
        controller = BotController()
        controller.show_status()
        return
    if cmd == "close" and len(sys.argv) >= 3:
        controller = BotController()
        controller.manual_close(sys.argv[2].upper())
        return
    if cmd == "logs":
        controller = BotController()
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 20
        controller.show_logs(n)
        return

    # Start needs full init
    if not ping():
        logger.error("Cannot connect to Binance")
        sys.exit(1)
    logger.info("Network check passed")

    controller = BotController()

    if cmd == "start":
        controller.start()
        try:
            while controller.running:
                time.sleep(1)
        except KeyboardInterrupt:
            controller.stop()
    elif cmd == "stop":
        controller.stop()
    else:
        print("Commands: start | stop | status | close SYMBOL | logs N")


if __name__ == "__main__":
    main()
