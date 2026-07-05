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
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger
from src.binance_client import get_current_price, ping
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import Position as PositionModel
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10
MAX_CANDLES = 100  # Buffer size per symbol

STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"


class NetworkWatchdog:
    def __init__(self, controller: "BotController"):
        self.controller = controller
        self._stop_event = threading.Event()
        self.ping_failures = 0
        self.ws_disconnects: list[datetime] = []

    def start(self):
        self._stop_event.clear()
        threading.Thread(target=self._run, daemon=True).start()

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
                logger.critical("NETWORK LOST")
                self.controller.stop()
                return
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=WS_RECONNECT_WINDOW_MIN
            )
            self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]

    def record_ws_disconnect(self):
        self.ws_disconnects.append(datetime.now(timezone.utc))
        if len(self.ws_disconnects) >= MAX_WS_RECONNECTS:
            logger.critical("NETWORK UNSTABLE")
            self.controller.stop()


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
        self._candles: dict[str, deque[dict]] = {}  # symbol → candle buffer
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol)
            self._candles[symbol] = deque(maxlen=MAX_CANDLES)

    def _write_status(self):
        data = {
            "running": self.running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "network_ok": ping(),
            "ws_disconnects_10m": len(self.watchdog.ws_disconnects),
            "total_trades": self._total_trades,
            **(self.risk_manager.get_daily_stats()),
        }
        STATUS_FILE.write_text(json.dumps(data, indent=2))

    def start(self):
        if self.running:
            return
        if not ping():
            print("ERROR: Cannot connect to Binance")
            return
        self.running = True
        self._started_at = datetime.now(timezone.utc)
        self.watchdog.start()
        self._write_status()
        logger.info("=== Trading Bot STARTED ===")
        # Load initial historical candles
        self._load_initial_candles()
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
        self._write_status()
        logger.info("=== Trading Bot STOPPED ===")

    def _load_initial_candles(self):
        """Fetch last 50 candles via REST to prime the buffer."""
        from src.binance_client import get_client

        client = get_client()
        for symbol in self._candles:
            try:
                klines = client.get_klines(symbol=symbol, interval="5m", limit=50)
                for k in klines:
                    self._candles[symbol].append(
                        {
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                        }
                    )
                logger.info(f"Loaded {len(klines)} historical candles for {symbol}")
            except Exception as e:
                logger.error(f"Failed to load candles for {symbol}: {e}")

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
        running = False
        if STATUS_FILE.exists():
            try:
                running = json.loads(STATUS_FILE.read_text()).get("running", False)
            except Exception:
                pass
        ping_ok = ping()
        print(f"\n{'=' * 40}")
        print(" Trading Bot Status")
        print(f"{'=' * 40}")
        print(f" Running: {running}")
        print(f" Network: {'OK' if ping_ok else 'FAIL'}")
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
        # Buffer sizes
        print(
            f"\n Candle buffers: "
            + " | ".join(
                f"{s}={len(self._candles.get(s, []))}" for s in self.strategies
            )
        )
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
        symbol = kline.get("s", "")
        if symbol not in self.strategies:
            return

        # Only use closed candles for trading decisions
        is_closed = kline.get("x", False)
        candle = {
            "open": float(kline.get("o", 0)),
            "high": float(kline.get("h", 0)),
            "low": float(kline.get("l", 0)),
            "close": float(kline.get("c", 0)),
            "volume": float(kline.get("v", 0)),
        }

        # Update buffer (replace or append)
        buf = self._candles[symbol]
        if is_closed:
            if (
                buf
                and buf[-1]["close"] == candle["close"]
                and buf[-1]["open"] == candle["open"]
            ):
                buf[-1] = candle  # Update last
            else:
                buf.append(candle)
        else:
            # Live candle: update last if matching time, else append
            if buf:
                buf[-1] = candle
            else:
                buf.append(candle)

        if not is_closed:
            return  # Only trade on closed candles

        if len(buf) < 25:
            return  # Need enough data

        allowed, _ = self.risk_manager.is_trading_allowed()
        if not allowed:
            return

        close_price = candle["close"]
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

            if pos.take_profit and close_price >= float(pos.take_profit):
                self.executor.close_position(pos, close_price, reason="take_profit")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                self._total_trades += 1
                logger.info(
                    f"TP {symbol} @ {close_price:.2f} PnL={pos.unrealized_pnl:.2f}"
                )
                return
            if pos.stop_loss and close_price <= float(pos.stop_loss):
                self.executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(float(pos.unrealized_pnl))
                self._total_trades += 1
                logger.info(
                    f"SL {symbol} @ {close_price:.2f} PnL={pos.unrealized_pnl:.2f}"
                )
                return
        else:
            df = pd.DataFrame(list(buf))
            signal = self.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                logger.info(
                    f"Signal: {signal.signal.value} {symbol} @ {signal.price:.2f} "
                    f"R={signal.reason} SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f}"
                )
                ok = self.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if ok:
                    self._total_trades += 1

        self._write_status()


def main():
    setup_logging()
    init_db()

    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""

    if cmd == "status":
        BotController().show_status()
        return
    if cmd == "close" and len(sys.argv) >= 3:
        BotController().manual_close(sys.argv[2].upper())
        return
    if cmd == "logs":
        BotController().show_logs(int(sys.argv[2]) if len(sys.argv) >= 3 else 20)
        return

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
