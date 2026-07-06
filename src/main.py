"""
Trading Bot — REST polling mode (reliable kline fetching).
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
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import AccountSnapshot, BotLog, MarketData
from src.models import Position as PM
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

INTERVAL = "1m"
POLL_SEC = 15
PING_S = 30
MAX_PF = 3
MAX_CANDLES = 100
PRICE_CRASH_CANDLES = 60
SNAP_S = 6 * 3600
CFG_RELOAD_S = 300

STATUS_FILE = Path(__file__).resolve().parents[1] / "bot_status.json"
PROFIT_BTC_FILE = Path(__file__).resolve().parents[1] / "profit_btc.json"
PROFIT_USDT_FILE = Path(__file__).resolve().parents[1] / "profit_usdt.json"


class Nwd:
    def __init__(self, c):
        self.c = c
        self.e = threading.Event()
        self.pf = 0
        self.dc = []

    def start(self):
        self.e.clear()
        threading.Thread(target=self._r, daemon=True).start()

    def stop(self):
        self.e.set()

    def _r(self):
        while not self.e.is_set():
            self.e.wait(PING_S)
            if self.e.is_set():
                break
            if ping():
                self.pf = 0
            else:
                self.pf += 1
                logger.warning(f"Ping fail {self.pf}/{MAX_PF}")
            if self.pf >= MAX_PF:
                logger.critical("NET LOST")
                self.c.stop()
                return


class BCtrl:
    def __init__(self):
        self.running = False
        self.risk = RiskManager()
        self.exec = TradeExecutor()
        self.wd = Nwd(self)
        self.started_at = None
        self.trades = 0
        self.pbtc = 0.0
        self.pusdt = 0.0
        self.candles: dict[str, deque[dict]] = {}
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.last_snap = datetime.min.replace(tzinfo=timezone.utc)
        self.last_cfg = datetime.min.replace(tzinfo=timezone.utc)
        self._last_close_times: dict[str, int] = {}
        cfg = get_config()
        for s in cfg.bot.symbols:
            self.candles[s] = deque(maxlen=MAX_CANDLES)
            self.strategies[s] = BollingerRSIStrategy(symbol=s, interval=INTERVAL)
        self._load_profit()

    def _load_profit(self):
        if PROFIT_BTC_FILE.exists():
            try:
                self.pbtc = json.loads(PROFIT_BTC_FILE.read_text()).get("total_btc", 0.0)
            except Exception:
                pass

    def _save_profit(self):
        PROFIT_BTC_FILE.write_text(
            json.dumps(
                {
                    "total_btc": round(self.pbtc, 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fix_profit(self, p):
        bp = get_current_price("BTCUSDT")
        if bp <= 0:
            return
        btc = p / bp
        self.pbtc += btc
        self._save_profit()
        logger.info(f"PROFIT: {p:.2f} USDT -> {btc:.8f} BTC (total: {self.pbtc:.8f})")
        s = get_session()
        s.add(
            BotLog(
                level="INFO",
                category="profit",
                message=f"+{p:.2f} USDT -> {btc:.8f} BTC | Total: {self.pbtc:.8f} BTC",
            )
        )
        s.commit()
        s.close()

    def _snap(self):
        try:
            bal = get_account_balance("USDT")
            s = get_session()
            s.add(
                AccountSnapshot(
                    total_balance=bal,
                    available_balance=bal,
                    locked_balance=0,
                    snapshot_time=datetime.now(timezone.utc),
                    balances_json={"profit_btc": self.pbtc, "profit_usdt": self.pusdt},
                )
            )
            s.commit()
            s.close()
            self.last_snap = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {bal:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot: {e}")

    def _save_candle_db(self, sym, k, c):
        try:
            s = get_session()
            ot = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc)
            if (
                not s.query(MarketData)
                .filter(
                    MarketData.symbol == sym,
                    MarketData.interval == INTERVAL,
                    MarketData.open_time == ot,
                )
                .first()
            ):
                s.add(
                    MarketData(
                        symbol=sym,
                        interval=INTERVAL,
                        open_time=ot,
                        close_time=datetime.fromtimestamp(k["T"] / 1000, tz=timezone.utc),
                        open=c["open"],
                        high=c["high"],
                        low=c["low"],
                        close=c["close"],
                        volume=c["volume"],
                        trades_count=k.get("n", 0),
                    )
                )
                s.commit()
            s.close()
        except Exception as e:
            logger.error(f"Save candle {sym}: {e}")

    def _wstatus(self):
        STATUS_FILE.write_text(
            json.dumps(
                {
                    "running": self.running,
                    "started_at": self.started_at.isoformat() if self.started_at else None,
                    "network_ok": ping(),
                    "trades": self.trades,
                    "profit_btc": round(self.pbtc, 8),
                    "profit_usdt": round(self.pusdt, 4),
                    **(self.risk.get_daily_stats()),
                },
                indent=2,
            )
        )

    def start(self):
        if self.running:
            return
        if not ping():
            print("ERROR: Binance unreachable")
            return
        self.running = True
        self.started_at = datetime.now(timezone.utc)
        self.wd.start()
        self.risk.day_start_balance = get_account_balance("USDT")
        self._wstatus()
        self._load_candles()
        self._snap()
        logger.info("=== Bot STARTED (REST polling) ===")
        self._poll_loop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.wd.stop()
        self._snap()
        self._wstatus()
        logger.info("=== Bot STOPPED ===")

    def _load_candles(self):
        cl = get_client()
        for sym in self.candles:
            try:
                kls = cl.get_klines(symbol=sym, interval=INTERVAL, limit=50)
                logger.info(
                    f"Poll {sym}: got {len(kls)} klines, "
                    f"last_ct={self._last_close_times.get(sym, 0)} "
                    f"newest_ct={kls[-1][6] if kls else 0}"
                )
                for k in kls:
                    self.candles[sym].append(
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
                self._last_close_times[sym] = kls[-1][6] if kls else 0
                logger.info(f"Loaded {len(kls)} candles {sym}")
            except Exception as e:
                logger.error(f"Load candles {sym}: {e}")

    def _poll_loop(self):
        cl = get_client()
        while self.running:
            try:
                for sym in self.candles:
                    kls = cl.get_klines(symbol=sym, interval=INTERVAL, limit=3)
                    logger.info(
                        f"Poll {sym}: got {len(kls)} klines, "
                        f"last_ct={self._last_close_times.get(sym, 0)} "
                        f"newest_ct={kls[-1][6] if kls else 0}"
                    )
                    for k in kls:
                        ct = k[6]
                        if ct <= self._last_close_times.get(sym, 0):
                            continue
                        self._last_close_times[sym] = ct
                        msg = {
                            "k": {
                                "t": k[0],
                                "T": k[6],
                                "s": sym,
                                "o": k[1],
                                "h": k[2],
                                "l": k[3],
                                "c": k[4],
                                "v": k[5],
                                "n": k[8],
                                "x": True,
                            }
                        }
                        self._on_kline(msg)
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(POLL_SEC)

    def _on_kline(self, msg):
        k = msg["k"]
        sym = k["s"]
        c = {
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        buf = self.candles[sym]
        if buf and buf[-1].get("open_time") == k["t"]:
            buf[-1] = c
        else:
            buf.append(c)
        self._save_candle_db(sym, k, c)

        if len(buf) < 25:
            return

        # Drawdown check
        if self.risk.day_start_balance > 0:
            self.risk.check_daily_drawdown(get_account_balance("USDT"))
        # Breaker: ATR
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / c["close"]) * 100 > 3.0:
                self.risk.check_atr_volatility((atr / c["close"]) * 100)
        # Breaker: price crash
        if len(buf) >= PRICE_CRASH_CANDLES:
            self.risk.check_price_crash(
                sym, c["close"], list(buf)[-PRICE_CRASH_CANDLES]["close"]
            )
        if not self.risk.is_trading_allowed()[0]:
            return

        price = c["close"]
        s = get_session()
        pos = s.query(PM).filter(PM.symbol == sym, PM.status == "OPEN").first()
        if pos:
            qty = float(pos.quantity)
            pos.current_price = price
            pos.unrealized_pnl = (price - float(pos.entry_price)) * qty
            s.commit()
            if pos.take_profit and price >= float(pos.take_profit):
                pnl = float(pos.unrealized_pnl)
                s.close()
                self.exec.close_position(pos, price, "take_profit")
                self.risk.record_trade(pnl)
                self.trades += 1
                logger.info(f"TP {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit(pnl)
                return
            if pos.stop_loss and price <= float(pos.stop_loss):
                pnl = float(pos.unrealized_pnl)
                s.close()
                self.exec.close_position(pos, price, "stop_loss")
                self.risk.record_trade(pnl)
                self.trades += 1
                logger.info(f"SL {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix_profit(pnl)
                return
            s.close()
        else:
            s.close()
            df = pd.DataFrame(list(buf))
            sig = self.strategies[sym].analyze(df)
            if sig.signal.value == "BUY":
                logger.info(
                    f"Signal BUY {sym} @ {sig.price:.2f} ({sig.reason}) "
                    f"SL={sig.stop_loss:.2f} TP={sig.take_profit:.2f}"
                )
                if self.exec.open_position(sym, sig.price, sig.stop_loss, sig.take_profit):
                    self.trades += 1
        self._wstatus()
        if (datetime.now(timezone.utc) - self.last_snap).total_seconds() > SNAP_S:
            self._snap()

    def mclose(self, sym):
        s = get_session()
        pos = s.query(PM).filter(PM.symbol == sym, PM.status == "OPEN").first()
        if not pos:
            s.close()
            print(f"No position: {sym}")
            return False
        s.close()
        return self.exec.close_position(pos, get_current_price(sym), reason="manual")

    def sstatus(self):
        d = self.risk.get_daily_stats()
        s = get_session()
        pos = s.query(PM).filter(PM.status == "OPEN").all()
        s.close()
        run = False
        pbtc = 0.0
        pusdt = 0.0
        if STATUS_FILE.exists():
            try:
                j = json.loads(STATUS_FILE.read_text())
                run = j.get("running", False)
                pbtc = j.get("profit_btc", 0.0)
                pusdt = j.get("profit_usdt", 0.0)
            except Exception:
                pass
        bal = get_account_balance("USDT")
        print(f"\n{'=' * 45}\n  Bot Status ({INTERVAL})\n{'=' * 45}")
        print(f"  Running: {run} | Network: {'OK' if ping() else 'FAIL'}")
        print(f"  Balance: {bal:.2f} USDT | Profit: {pbtc:.8f} BTC")
        print(f"  Trades: {d['trades']} | PnL: {d['pnl']} USDT")
        print(f"  Loss streak: {d['consecutive_losses']} | Breaker: {d['breaker']}")
        if pos:
            print(f"\n  Positions ({len(pos)}):")
            for p in pos:
                print(
                    f"    {p.symbol} entry={float(p.entry_price):.2f} "
                    f"qty={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n  Positions: 0")
        print(f"{'=' * 45}\n")

    def slogs(self, n=20):
        s = get_session()
        logs = s.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        s.close()
        for e in reversed(logs):
            print(f"[{e.level}] [{e.category or '-'}] {e.message}")


def main():
    setup_logging()
    init_db()
    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""
    if cmd == "status":
        BCtrl().sstatus()
        return
    if cmd == "close" and len(sys.argv) >= 3:
        BCtrl().mclose(sys.argv[2].upper())
        return
    if cmd == "logs":
        BCtrl().slogs(int(sys.argv[2]) if len(sys.argv) >= 3 else 20)
        return
    if not ping():
        logger.error("Cannot connect to Binance")
        sys.exit(1)
    logger.info("Network OK")
    c = BCtrl()
    if cmd == "start":
        c.start()
        try:
            while c.running:
                time.sleep(1)
        except KeyboardInterrupt:
            c.stop()
    elif cmd == "stop":
        c.stop()
    else:
        print("Commands: start | stop | status | close SYMBOL | logs N")


if __name__ == "__main__":
    main()
