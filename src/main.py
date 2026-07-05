"""
Trading Bot — main controller (full requirements).

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
        self.c = controller
        self._stop = threading.Event()
        self.pf = 0
        self.ws_dc: list[datetime] = []

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
                self.pf = 0
            else:
                self.pf += 1
                logger.warning(f"Ping fail ({self.pf}/{MAX_PING_FAILURES})")
            if self.pf >= MAX_PING_FAILURES:
                logger.critical("NETWORK LOST")
                self.c.stop()
                return
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=WS_RECONNECT_WINDOW_MIN
            )
            self.ws_dc = [t for t in self.ws_dc if t > cutoff]

    def record_ws_disconnect(self):
        self.ws_dc.append(datetime.now(timezone.utc))
        if len(self.ws_dc) >= MAX_WS_RECONNECTS:
            logger.critical("NETWORK UNSTABLE")
            self.c.stop()


class BotController:
    def __init__(self):
        self.running = False
        self.risk = RiskManager()
        self.exec = TradeExecutor()
        self.wd = NetworkWatchdog(self)
        self._ws = None
        self._start: datetime | None = None
        self._trades = 0
        self._profit_btc = 0.0
        self._candles: dict[str, deque[dict]] = {}
        self._prices: dict[str, float] = {}
        self._last_snap = datetime.min.replace(tzinfo=timezone.utc)
        self._last_config_reload = datetime.min.replace(tzinfo=timezone.utc)
        cfg = get_config()
        for s in cfg.bot.symbols:
            self._candles[s] = deque(maxlen=MAX_CANDLES)

    # ----- Profit (BTC) -----
    def _load_profit(self):
        if PROFIT_FILE.exists():
            try:
                self._profit_btc = json.loads(PROFIT_FILE.read_text()).get(
                    "total_btc", 0.0
                )
            except Exception:
                pass

    def _save_profit(self):
        PROFIT_FILE.write_text(
            json.dumps(
                {
                    "total_btc": round(self._profit_btc, 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fix_profit(self, profit_usdt: float):
        btc_price = get_current_price("BTCUSDT")
        if btc_price <= 0:
            return
        btc = profit_usdt / btc_price
        self._profit_btc += btc
        self._save_profit()
        logger.info(
            f"PROFIT: {profit_usdt:.2f} USDT → {btc:.8f} BTC (total: {self._profit_btc:.8f})"
        )
        s = get_session()
        s.add(
            BotLog(
                level="INFO",
                category="profit",
                message=f"Profit: {profit_usdt:.2f} USDT → {btc:.8f} BTC | Total: {self._profit_btc:.8f} BTC",
            )
        )
        s.commit()

    # ----- Snapshots -----
    def _snap(self):
        try:
            t = get_account_balance("USDT")
            s = get_session()
            s.add(
                AccountSnapshot(
                    total_balance=t,
                    available_balance=t,
                    locked_balance=0,
                    snapshot_time=datetime.now(timezone.utc),
                    balances_json={"profit_btc": self._profit_btc},
                )
            )
            s.commit()
            self._last_snap = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {t:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot: {e}")

    # ----- Candle DB -----
    def _save_candle(self, symbol: str, kline: dict, c: dict):
        try:
            s = get_session()
            ot = datetime.fromtimestamp(kline.get("t", 0) / 1000, tz=timezone.utc)
            if (
                not s.query(MarketData)
                .filter(
                    MarketData.symbol == symbol,
                    MarketData.interval == "5m",
                    MarketData.open_time == ot,
                )
                .first()
            ):
                s.add(
                    MarketData(
                        symbol=symbol,
                        interval="5m",
                        open_time=ot,
                        close_time=datetime.fromtimestamp(
                            kline.get("T", 0) / 1000, tz=timezone.utc
                        ),
                        open=c["open"],
                        high=c["high"],
                        low=c["low"],
                        close=c["close"],
                        volume=c["volume"],
                        trades_count=kline.get("n", 0),
                    )
                )
                s.commit()
        except Exception as e:
            logger.error(f"Candle save {symbol}: {e}")

    # ----- Status -----
    def _wstatus(self):
        STATUS_FILE.write_text(
            json.dumps(
                {
                    "running": self.running,
                    "started_at": self._start.isoformat() if self._start else None,
                    "network_ok": ping(),
                    "ws_dc": len(self.wd.ws_dc),
                    "trades": self._trades,
                    "profit_btc": round(self._profit_btc, 8),
                    **(self.risk.get_daily_stats()),
                },
                indent=2,
            )
        )

    # ----- Lifecycle -----
    def start(self):
        if self.running:
            return
        if not ping():
            print("ERROR: Binance unreachable")
            return
        self.running = True
        self._start = datetime.now(timezone.utc)
        self._load_profit()
        self.wd.start()
        self._wstatus()
        # Initialize day balance for drawdown check (FR-3.6)
        self.risk.day_start_balance = get_account_balance("USDT")
        logger.info("=== Trading Bot STARTED ===")
        self._load_candles()
        self._snap()
        self._ws_start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.wd.stop()
        if self._ws:
            try:
                self._ws.stop()
            except Exception:
                pass
            self._ws = None
        self._snap()
        self._wstatus()
        logger.info("=== Trading Bot STOPPED ===")

    def _load_candles(self):
        client = get_client()
        for sym, buf in self._candles.items():
            try:
                for k in client.get_klines(symbol=sym, interval="5m", limit=50):
                    buf.append(
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
                logger.info(f"Loaded {len(buf)} candles for {sym}")
            except Exception as e:
                logger.error(f"Candles {sym}: {e}")

    def manual_close(self, symbol: str) -> bool:
        s = get_session()
        pos = (
            s.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )
        if not pos:
            print(f"No position: {symbol}")
            return False
        return self.exec.close_position(pos, get_current_price(symbol), reason="manual")

    def show_status(self):
        d = self.risk.get_daily_stats()
        s = get_session()
        pos = s.query(PositionModel).filter(PositionModel.status == "OPEN").all()
        run = False
        pbtc = 0.0
        if STATUS_FILE.exists():
            try:
                j = json.loads(STATUS_FILE.read_text())
                run = j.get("running", False)
                pbtc = j.get("profit_btc", 0.0)
            except Exception:
                pass
        bal = get_account_balance("USDT")
        print(f"\n{'=' * 45}\n  Trading Bot Status\n{'=' * 45}")
        print(f"  Running: {run} | Network: {'OK' if ping() else 'FAIL'}")
        print(f"  Balance: {bal:.2f} USDT | Profit: {pbtc:.8f} BTC")
        print(f"  Day trades: {d['trades']} | Day PnL: {d['pnl']} USDT")
        print(f"  Loss streak: {d['consecutive_losses']} | Breaker: {d['breaker']}")
        if pos:
            print(f"\n  Positions ({len(pos)}):")
            for p in pos:
                print(
                    f"    {p.symbol} entry={float(p.entry_price):.2f} qty={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n  Positions: 0")
        print(
            f"  Buffers: "
            + " | ".join(f"{s}={len(self._candles.get(s, []))}" for s in self._candles)
        )
        print(f"{'=' * 45}\n")

    def show_logs(self, n=20):
        for e in reversed(
            get_session()
            .query(BotLog)
            .order_by(BotLog.created_at.desc())
            .limit(n)
            .all()
        ):
            print(f"[{e.level}] [{e.category or '-'}] {e.message}")

    # ----- WebSocket -----
    def _ws_start(self):
        cfg = get_config()
        try:
            from binance import ThreadedWebsocketManager

            self._ws = ThreadedWebsocketManager(
                api_key=cfg.binance.api_key,
                api_secret=cfg.binance.api_secret,
                testnet=True,
            )
            self._ws.start()
            self._ws.start_multiplex_socket(
                callback=self._on_kline,
                streams=[f"{s.lower()}@kline_5m" for s in cfg.bot.symbols],
            )
            logger.info(f"WebSocket: {cfg.bot.symbols}")
            self._ws.join()
        except Exception as e:
            logger.error(f"WebSocket: {e}")
            self.wd.record_ws_disconnect()

    def _on_kline(self, msg: dict):
        if not self.running:
            return
        d = msg.get("data", {})
        k = d.get("k", {})
        sym = k.get("s", "")
        if sym not in self._candles:
            return

        closed = k.get("x", False)
        candle = {
            "open": float(k.get("o", 0)),
            "high": float(k.get("h", 0)),
            "low": float(k.get("l", 0)),
            "close": float(k.get("c", 0)),
            "volume": float(k.get("v", 0)),
        }
        self._prices[sym] = candle["close"]
        buf = self._candles[sym]

        if closed:
            if (
                buf
                and buf[-1]["close"] == candle["close"]
                and buf[-1]["open"] == candle["open"]
            ):
                buf[-1] = candle
            else:
                buf.append(candle)
            self._save_candle(sym, k, candle)
        else:
            if buf:
                buf[-1] = candle
            else:
                buf.append(candle)
        if not closed or len(buf) < 25:
            return

        # FR-3.6: Daily drawdown check (Orange breaker)
        if self.risk.day_start_balance > 0:
            cur_bal = get_account_balance("USDT")
            self.risk.check_daily_drawdown(cur_bal)

        # FR-4.7: Reload config from DB every 5 minutes
        cfg = get_config()
        if (
            datetime.now(timezone.utc) - self._last_config_reload
        ).total_seconds() > 300:
            reload_from_db()
            self._last_config_reload = datetime.now(timezone.utc)
            # Update strict RSI filter on strategies
            for strat in self._candles:
                if strat in self._candles:
                    pass  # Strategies are recreated on config change in full version

        # Circuit Breaker
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / candle["close"]) * 100 > 3.0:
                self.risk.check_atr_volatility((atr / candle["close"]) * 100)
        if len(buf) >= 12:
            self.risk.check_price_crash(sym, candle["close"], list(buf)[-12]["close"])

        if not self.risk.is_trading_allowed()[0]:
            return

        price = candle["close"]
        s = get_session()
        pos = (
            s.query(PositionModel)
            .filter(PositionModel.symbol == sym, PositionModel.status == "OPEN")
            .first()
        )

        if pos:
            qty = float(pos.quantity)
            pos.current_price = price
            pos.unrealized_pnl = (price - float(pos.entry_price)) * qty
            s.commit()
            if pos.take_profit and price >= float(pos.take_profit):
                pnl = float(pos.unrealized_pnl)
                self.exec.close_position(pos, price, "take_profit")
                self.risk.record_trade(pnl)
                self._trades += 1
                logger.info(f"TP {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit(pnl)
                    return
            if pos.stop_loss and price <= float(pos.stop_loss):
                pnl = float(pos.unrealized_pnl)
                self.exec.close_position(pos, price, "stop_loss")
                self.risk.record_trade(pnl)
                self._trades += 1
                logger.info(f"SL {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit(pnl)
                    return
        else:
            df = pd.DataFrame(list(buf))
            signal = BollingerRSIStrategy(
                symbol=sym, use_strict_filter=cfg.bot.use_strict_rsi
            ).analyze(df)
            if signal.signal.value == "BUY":
                logger.info(
                    f"Signal BUY {sym} @ {signal.price:.2f} ({signal.reason}) SL={signal.stop_loss:.2f} TP={signal.take_profit:.2f}"
                )
                if self.exec.open_position(
                    sym, signal.price, signal.stop_loss, signal.take_profit
                ):
                    self._trades += 1

        self._wstatus()
        if (
            datetime.now(timezone.utc) - self._last_snap
        ).total_seconds() > SNAPSHOT_INTERVAL_SEC:
            self._snap()


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
