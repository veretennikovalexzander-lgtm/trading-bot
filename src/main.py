"""
Trading Bot — main controller (full requirements).

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
from src.binance_client import get_account_balance, get_client, get_current_price, ping
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import (
    AccountSnapshot,
    BotLog,
    MarketData,
)
from src.models import (
    Position as PositionModel,
)
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10
MAX_CANDLES = 100
SNAPSHOT_INTERVAL_SEC = 6 * 3600

STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"
PROFIT_FILE = Path(__file__).resolve().parents[1] / "profit_btc.json"


class NetworkWatchdog:
    def __init__(self, controller: BotController):
        self.controller = controller
        self._stop = threading.Event()
        self.ping_failures = 0
        self.ws_disconnects: list[datetime] = []

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(PING_INTERVAL_SEC)
            if self._stop.is_set():
                break
            if ping():
                self.ping_failures = 0
            else:
                self.ping_failures += 1
                logger.warning(
                    f"Ping failed ({self.ping_failures}/{MAX_PING_FAILURES})"
                )
            if self.ping_failures >= MAX_PING_FAILURES:
                logger.critical("NETWORK LOST — stopping")
                self.controller.stop()
                return
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=WS_RECONNECT_WINDOW_MIN
            )
            self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]

    def record_ws_disconnect(self):
        self.ws_disconnects.append(datetime.now(timezone.utc))
        if len(self.ws_disconnects) >= MAX_WS_RECONNECTS:
            logger.critical("NETWORK UNSTABLE — stopping")
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
        self._total_profit_btc = 0.0
        self._candles: dict[str, deque[dict]] = {}
        self._last_price: dict[str, float] = {}
        self._last_snapshot = datetime.min.replace(tzinfo=timezone.utc)
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(symbol=symbol)
            self._candles[symbol] = deque(maxlen=MAX_CANDLES)
        self._load_profit_state()

    # ----- Profit accumulation (BTC) -----

    def _load_profit_state(self):
        if PROFIT_FILE.exists():
            try:
                data = json.loads(PROFIT_FILE.read_text())
                self._total_profit_btc = data.get("total_btc", 0.0)
                logger.info(f"Loaded profit state: {self._total_profit_btc:.8f} BTC")
            except Exception:
                pass

    def _save_profit_state(self):
        PROFIT_FILE.write_text(
            json.dumps(
                {
                    "total_btc": round(self._total_profit_btc, 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fix_profit_to_btc(self, profit_usdt: float):
        """Convert excess USDT profit to BTC and accumulate."""
        btc_price = get_current_price("BTCUSDT")
        if btc_price <= 0:
            logger.error("Cannot get BTC price for profit fixation")
            return

        btc_amount = profit_usdt / btc_price
        self._total_profit_btc += btc_amount
        self._save_profit_state()

        logger.info(
            f"PROFIT FIXATION: {profit_usdt:.2f} USDT → {btc_amount:.8f} BTC "
            f"(total: {self._total_profit_btc:.8f} BTC)"
        )

        session = get_session()
        log = BotLog(
            level="INFO",
            category="profit",
            message=f"Profit: {profit_usdt:.2f} USDT → {btc_amount:.8f} BTC | Total: {self._total_profit_btc:.8f} BTC",
        )
        session.add(log)
        session.commit()

    # ----- Snapshots (FR-3.7, FR-4.5) -----

    def _take_snapshot(self):
        try:
            total = get_account_balance("USDT")
            session = get_session()
            snap = AccountSnapshot(
                total_balance=total,
                available_balance=total,
                locked_balance=0,
                snapshot_time=datetime.now(timezone.utc),
                balances_json={"total_profit_btc": self._total_profit_btc},
            )
            session.add(snap)
            session.commit()
            self._last_snapshot = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {total:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot failed: {e}")

    # ----- Candle DB (FR-4.3) -----

    def _save_candle_to_db(self, symbol: str, kline: dict, candle: dict):
        try:
            session = get_session()
            ot = datetime.fromtimestamp(kline.get("t", 0) / 1000, tz=timezone.utc)
            existing = (
                session.query(MarketData)
                .filter(
                    MarketData.symbol == symbol,
                    MarketData.interval == "5m",
                    MarketData.open_time == ot,
                )
                .first()
            )
            if not existing:
                md = MarketData(
                    symbol=symbol,
                    interval="5m",
                    open_time=ot,
                    close_time=datetime.fromtimestamp(
                        kline.get("T", 0) / 1000, tz=timezone.utc
                    ),
                    open=candle["open"],
                    high=candle["high"],
                    low=candle["low"],
                    close=candle["close"],
                    volume=candle["volume"],
                    trades_count=kline.get("n", 0),
                )
                session.add(md)
                session.commit()
        except Exception as e:
            logger.error(f"Failed to save candle {symbol}: {e}")

    # ----- Status file -----

    def _write_status(self):
        data = {
            "running": self.running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "network_ok": ping(),
            "ws_disconnects_10m": len(self.watchdog.ws_disconnects),
            "total_trades": self._total_trades,
            "total_profit_btc": round(self._total_profit_btc, 8),
            **(self.risk_manager.get_daily_stats()),
        }
        STATUS_FILE.write_text(json.dumps(data, indent=2))

    # ----- Lifecycle -----

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
        self._load_initial_candles()
        self._take_snapshot()
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
        self._take_snapshot()
        self._write_status()
        logger.info("=== Trading Bot STOPPED ===")

    def _load_initial_candles(self):
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
                            "open_time": k[0],
                            "close_time": k[6],
                        }
                    )
                logger.info(f"Loaded {len(klines)} candles for {symbol}")
            except Exception as e:
                logger.error(f"Candles load failed {symbol}: {e}")

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
        profit_btc = 0.0
        if STATUS_FILE.exists():
            try:
                d = json.loads(STATUS_FILE.read_text())
                running = d.get("running", False)
                profit_btc = d.get("total_profit_btc", 0.0)
            except Exception:
                pass
        print(f"\n{'=' * 45}")
        print("  Trading Bot Status")
        print(f"{'=' * 45}")
        print(f"  Running:       {running}")
        print(f"  Network:       {'OK' if ping() else 'FAIL'}")
        print(f"  Balance:       {get_account_balance('USDT'):.2f} USDT")
        print(f"  Profit (BTC):  {profit_btc:.8f} BTC")
        print(f"  Daily trades:  {daily['trades']} | PnL: {daily['pnl']} USDT")
        print(
            f"  Loss streak:   {daily['consecutive_losses']} | Breaker: {daily['breaker']}"
        )
        if positions:
            print(f"\n  Open positions ({len(positions)}):")
            for p in positions:
                print(
                    f"    {p.symbol} entry={float(p.entry_price):.2f} qty={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n  Open positions: 0")
        print(
            f"  Buffers: "
            + " | ".join(
                f"{s}={len(self._candles.get(s, []))}" for s in self.strategies
            )
        )
        print(f"{'=' * 45}\n")

    def show_logs(self, n: int = 20):
        session = get_session()
        logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        for e in reversed(logs):
            print(f"[{e.level}] [{e.category or '-'}] {e.message}")

    # ----- WebSocket -----

    def _init_websocket(self):
        cfg = get_config()
        try:
            from binance import ThreadedWebsocketManager

            self._ws_twm = ThreadedWebsocketManager(
                api_key=cfg.binance.api_key,
                api_secret=cfg.binance.api_secret,
                testnet=True,
            )
            self._ws_twm.start()
            streams = [f"{s.lower()}@kline_5m" for s in cfg.bot.symbols]
            self._ws_twm.start_multiplex_socket(
                callback=self._on_kline, streams=streams
            )
            logger.info(f"WebSocket: {cfg.bot.symbols}")
            self._ws_twm.join()
        except Exception as e:
            logger.error(f"WebSocket: {e}")
            self.watchdog.record_ws_disconnect()

    def _on_kline(self, msg: dict):
        if not self.running:
            return
        data = msg.get("data", {})
        kline = data.get("k", {})
        symbol = kline.get("s", "")
        if symbol not in self.strategies:
            return

        is_closed = kline.get("x", False)
        candle = {
            "open": float(kline.get("o", 0)),
            "high": float(kline.get("h", 0)),
            "low": float(kline.get("l", 0)),
            "close": float(kline.get("c", 0)),
            "volume": float(kline.get("v", 0)),
        }
        self._last_price[symbol] = candle["close"]

        buf = self._candles[symbol]
        if is_closed:
            if (
                buf
                and buf[-1]["close"] == candle["close"]
                and buf[-1]["open"] == candle["open"]
            ):
                buf[-1] = candle
            else:
                buf.append(candle)
            self._save_candle_to_db(symbol, kline, candle)
        else:
            if buf:
                buf[-1] = candle
            else:
                buf.append(candle)
        if not is_closed or len(buf) < 25:
            return

        # --- Circuit Breaker ---
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr_val = (df["high"] - df["low"]).tail(14).mean()
            atr_pct = (atr_val / candle["close"]) * 100
            if atr_pct > 3.0 and self.risk_manager.breaker_level.value == "NONE":
                self.risk_manager.check_atr_volatility(atr_pct)
        if len(buf) >= 12:
            price_1h_ago = list(buf)[-12]["close"]
            self.risk_manager.check_price_crash(symbol, candle["close"], price_1h_ago)

        allowed, _ = self.risk_manager.is_trading_allowed()
        if not allowed:
            return

        # --- Position management ---
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
                pnl = float(pos.unrealized_pnl)
                self.executor.close_position(pos, close_price, reason="take_profit")
                self.risk_manager.record_trade(pnl)
                self._total_trades += 1
                logger.info(f"TP {symbol} @ {close_price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit_to_btc(pnl)
                return
            if pos.stop_loss and close_price <= float(pos.stop_loss):
                pnl = float(pos.unrealized_pnl)
                self.executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(pnl)
                self._total_trades += 1
                logger.info(f"SL {symbol} @ {close_price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit_to_btc(pnl)
                return
        else:
            df = pd.DataFrame(list(buf))
            signal = self.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                logger.info(
                    f"Signal BUY {symbol} @ {signal.price:.2f} "
                    f"({signal.reason}) SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f}"
                )
                ok = self.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if ok:
                    self._total_trades += 1

        self._write_status()

        # Periodic snapshot
        if (
            datetime.now(timezone.utc) - self._last_snapshot
        ).total_seconds() > SNAPSHOT_INTERVAL_SEC:
            self._take_snapshot()


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
