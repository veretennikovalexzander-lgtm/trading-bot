"""
Trading Bot — main controller (all fixes applied).
Usage: python -m src.main [start|stop|status|close SYMBOL|logs N]
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
from src.config import get_config, reload_from_db
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import AccountSnapshot, BotLog, MarketData
from src.models import Position as PositionModel
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

INTERVAL = "1m"
PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10
MAX_CANDLES = 100
PRICE_CRASH_CANDLES = 60  # 60 x 1m = 1 hour (was 12 for 5m)
SNAPSHOT_INTERVAL_SEC = 6 * 3600
CONFIG_RELOAD_SEC = 300

STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"
PROFIT_FILE = Path(__file__).resolve().parents[1] / "profit_btc.json"


class NetworkWatchdog:
    def __init__(self, controller):
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
                logger.warning(f"Ping fail ({self.ping_failures}/{MAX_PING_FAILURES})")
            if self.ping_failures >= MAX_PING_FAILURES:
                logger.critical("NETWORK LOST — stopping bot")
                self.controller.stop()
                return
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=WS_RECONNECT_WINDOW_MIN
            )
            self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]

    def record_ws_disconnect(self):
        self.ws_disconnects.append(datetime.now(timezone.utc))
        if len(self.ws_disconnects) >= MAX_WS_RECONNECTS:
            logger.critical("NETWORK UNSTABLE — stopping bot")
            self.controller.stop()


class BotController:
    def __init__(self):
        self.running = False
        self.risk_manager = RiskManager()
        self.trade_executor = TradeExecutor()
        self.watchdog = NetworkWatchdog(self)
        self.websocket_manager = None
        self.started_at: datetime | None = None
        self.total_trades = 0
        self.total_profit_btc = 0.0
        self.candles: dict[str, deque[dict]] = {}
        self.last_prices: dict[str, float] = {}
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.last_snapshot = datetime.min.replace(tzinfo=timezone.utc)
        self.last_config_reload = datetime.min.replace(tzinfo=timezone.utc)
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.candles[symbol] = deque(maxlen=MAX_CANDLES)
            self.strategies[symbol] = BollingerRSIStrategy(
                symbol=symbol,
                interval=INTERVAL,
                use_strict_filter=cfg.bot.use_strict_rsi, debug=True,
            )
        self._load_profit()

    # ---- Profit (BTC) ----
    def _load_profit(self):
        if PROFIT_FILE.exists():
            try:
                self.total_profit_btc = json.loads(PROFIT_FILE.read_text()).get(
                    "total_btc", 0.0
                )
            except Exception:
                pass

    def _save_profit(self):
        PROFIT_FILE.write_text(
            json.dumps(
                {
                    "total_btc": round(self.total_profit_btc, 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fix_profit_to_btc(self, profit_usdt: float):
        btc_price = get_current_price("BTCUSDT")
        if btc_price <= 0:
            return
        btc_amount = profit_usdt / btc_price
        self.total_profit_btc += btc_amount
        self._save_profit()
        logger.info(
            f"PROFIT: {profit_usdt:.2f} USDT → {btc_amount:.8f} BTC (total: {self.total_profit_btc:.8f} BTC)"
        )
        session = get_session()
        session.add(
            BotLog(
                level="INFO",
                category="profit",
                message=f"+{profit_usdt:.2f} USDT → {btc_amount:.8f} BTC | Total: {self.total_profit_btc:.8f} BTC",
            )
        )
        session.commit()
        session.close()

    # ---- Snapshots ----
    def _take_snapshot(self):
        try:
            balance = get_account_balance("USDT")
            session = get_session()
            session.add(
                AccountSnapshot(
                    total_balance=balance,
                    available_balance=balance,
                    locked_balance=0,
                    snapshot_time=datetime.now(timezone.utc),
                    balances_json={"profit_btc": self.total_profit_btc},
                )
            )
            session.commit()
            session.close()
            self.last_snapshot = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {balance:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot failed: {e}")

    # ---- Candles to DB ----
    def _save_candle_to_db(self, symbol: str, kline: dict, candle: dict):
        try:
            session = get_session()
            open_time = datetime.fromtimestamp(
                kline.get("t", 0) / 1000, tz=timezone.utc
            )
            exists = (
                session.query(MarketData)
                .filter(
                    MarketData.symbol == symbol,
                    MarketData.interval == INTERVAL,
                    MarketData.open_time == open_time,
                )
                .first()
            )
            if not exists:
                session.add(
                    MarketData(
                        symbol=symbol,
                        interval=INTERVAL,
                        open_time=open_time,
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
                )
                session.commit()
            session.close()
        except Exception:
            pass

    # ---- Status file ----
    def _write_status_file(self):
        data = {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "network_ok": ping(),
            "ws_disconnects_10m": len(self.watchdog.ws_disconnects),
            "total_trades": self.total_trades,
            "profit_btc": round(self.total_profit_btc, 8),
            **(self.risk_manager.get_daily_stats()),
        }
        STATUS_FILE.write_text(json.dumps(data, indent=2))

    # ---- Lifecycle ----
    def start(self):
        if self.running:
            return
        if not ping():
            print("ERROR: Cannot connect to Binance")
            return
        self.running = True
        self.started_at = datetime.now(timezone.utc)
        self.watchdog.start()
        self.risk_manager.day_start_balance = get_account_balance("USDT")
        self._write_status_file()
        logger.info("=== Trading Bot STARTED ===")
        self._load_historical_candles()
        self._take_snapshot()
        self._start_websocket()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.watchdog.stop()
        if self.websocket_manager:
            try:
                self.websocket_manager.stop()
            except Exception:
                pass
            self.websocket_manager = None
        self._take_snapshot()
        self._write_status_file()
        logger.info("=== Trading Bot STOPPED ===")

    def _load_historical_candles(self):
        client = get_client()
        for symbol in self.candles:
            try:
                klines = client.get_klines(symbol=symbol, interval=INTERVAL, limit=50)
                for k in klines:
                    self.candles[symbol].append(
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
                logger.error(f"Failed to load candles for {symbol}: {e}")

    # ---- CLI ----
    def manual_close(self, symbol: str) -> bool:
        session = get_session()
        pos = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )
        if not pos:
            session.close()
            print(f"No open position for {symbol}")
            return False
        price = get_current_price(symbol)
        session.close()
        return self.trade_executor.close_position(pos, price, reason="manual")

    def show_status(self):
        daily = self.risk_manager.get_daily_stats()
        session = get_session()
        positions = (
            session.query(PositionModel).filter(PositionModel.status == "OPEN").all()
        )
        session.close()
        running = False
        profit_btc = 0.0
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                running = data.get("running", False)
                profit_btc = data.get("profit_btc", 0.0)
            except Exception:
                pass
        balance = get_account_balance("USDT")
        print(f"\n{'=' * 45}")
        print(f"  Trading Bot Status ({INTERVAL})")
        print(f"{'=' * 45}")
        print(f"  Running: {running} | Network: {'OK' if ping() else 'FAIL'}")
        print(f"  Balance: {balance:.2f} USDT | Profit: {profit_btc:.8f} BTC")
        print(f"  Daily trades: {daily['trades']} | Daily PnL: {daily['pnl']} USDT")
        print(
            f"  Loss streak: {daily['consecutive_losses']} | Breaker: {daily['breaker']}"
        )
        if positions:
            print(f"\n  Positions ({len(positions)}):")
            for p in positions:
                print(
                    f"    {p.symbol} entry={float(p.entry_price):.2f} qty={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n  Positions: 0")
        print(f"{'=' * 45}\n")

    def show_logs(self, n: int = 20):
        session = get_session()
        logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        session.close()
        for entry in reversed(logs):
            print(f"[{entry.level}] [{entry.category or '-'}] {entry.message}")

    # ---- WebSocket ----
    def _start_websocket(self):
        cfg = get_config()
        try:
            from binance import ThreadedWebsocketManager

            self.websocket_manager = ThreadedWebsocketManager(
                api_key=cfg.binance.api_key,
                api_secret=cfg.binance.api_secret,
                testnet=True,
            )
            self.websocket_manager.start()
            streams = [f"{s.lower()}@kline_{INTERVAL}" for s in cfg.bot.symbols]
            self.websocket_manager.start_multiplex_socket(
                callback=self._on_kline, streams=streams
            )
            logger.info(f"WebSocket {INTERVAL}: {cfg.bot.symbols}")
            self.websocket_manager.join()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.watchdog.record_ws_disconnect()

    def _on_kline(self, msg: dict):
        if not self.running:
            return
        data = msg.get("data", {})
        kline = data.get("k", {})
        symbol = kline.get("s", "")
        if symbol not in self.candles:
            return

        is_closed = kline.get("x", False)
        candle = {
            "open": float(kline.get("o", 0)),
            "high": float(kline.get("h", 0)),
            "low": float(kline.get("l", 0)),
            "close": float(kline.get("c", 0)),
            "volume": float(kline.get("v", 0)),
        }
        self.last_prices[symbol] = candle["close"]
        buf = self.candles[symbol]

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

        cfg = get_config()

        # Daily drawdown (FR-3.6)
        if self.risk_manager.day_start_balance > 0:
            self.risk_manager.check_daily_drawdown(get_account_balance("USDT"))

        # Config reload from DB (FR-4.7)
        if (
            datetime.now(timezone.utc) - self.last_config_reload
        ).total_seconds() > CONFIG_RELOAD_SEC:
            reload_from_db()
            self.last_config_reload = datetime.now(timezone.utc)

        # Circuit Breaker: ATR
        if len(buf) >= 15:
            df_atr = pd.DataFrame(list(buf))
            atr_val = (df_atr["high"] - df_atr["low"]).tail(14).mean()
            atr_pct = (atr_val / candle["close"]) * 100
            if atr_pct > 3.0:
                self.risk_manager.check_atr_volatility(atr_pct)

        # Circuit Breaker: Price crash (60 candles for 1m = 1 hour)
        if len(buf) >= PRICE_CRASH_CANDLES:
            price_n_candles_ago = list(buf)[-PRICE_CRASH_CANDLES]["close"]
            self.risk_manager.check_price_crash(
                symbol, candle["close"], price_n_candles_ago
            )

        if not self.risk_manager.is_trading_allowed()[0]:
            return

        # Position management
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
                realized_pnl = float(pos.unrealized_pnl)
                session.close()
                self.trade_executor.close_position(
                    pos, close_price, reason="take_profit"
                )
                self.risk_manager.record_trade(realized_pnl)
                self.total_trades += 1
                logger.info(f"TP {symbol} @ {close_price:.2f} PnL={realized_pnl:.2f}")
                if realized_pnl > 0:
                    self._fix_profit_to_btc(realized_pnl)
                return
            if pos.stop_loss and close_price <= float(pos.stop_loss):
                realized_pnl = float(pos.unrealized_pnl)
                session.close()
                self.trade_executor.close_position(pos, close_price, reason="stop_loss")
                self.risk_manager.record_trade(realized_pnl)
                self.total_trades += 1
                logger.info(f"SL {symbol} @ {close_price:.2f} PnL={realized_pnl:.2f}")
                if realized_pnl > 0:
                    self._fix_profit_to_btc(realized_pnl)
                return
            session.close()
        else:
            session.close()
            # Use pre-created strategy (not re-creating every candle)
            strategy = self.strategies[symbol]
            df = pd.DataFrame(list(buf))
            signal = strategy.analyze(df)
            if signal.signal.value == "BUY":
                logger.info(
                    f"Signal BUY {symbol} @ {signal.price:.2f} "
                    f"({signal.reason}) SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f}"
                )
                ok = self.trade_executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                )
                if ok:
                    self.total_trades += 1

        self._write_status_file()

        # Periodic snapshot
        if (
            datetime.now(timezone.utc) - self.last_snapshot
        ).total_seconds() > SNAPSHOT_INTERVAL_SEC:
            self._take_snapshot()


# ---- CLI Entry Point ----


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
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 20
        BotController().show_logs(n)
        return

    if not ping():
        logger.error("Cannot connect to Binance")
        sys.exit(1)
    logger.info("Network check passed")

    ctrl = BotController()
    if cmd == "start":
        ctrl.start()
        try:
            while ctrl.running:
                time.sleep(1)
        except KeyboardInterrupt:
            ctrl.stop()
    elif cmd == "stop":
        ctrl.stop()
    else:
        print("Commands: start | stop | status | close SYMBOL | logs N")


if __name__ == "__main__":
    main()
