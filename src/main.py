"""
Trading Bot — main controller. Interval: 1m for faster signals (test mode).
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
from src.models import Position as PM
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

INTERVAL = "1m"  # Faster signals for testing
PING_S = 30
MAX_PF = 3
MAX_WS = 5
WS_WIN = 10
MAX_C = 100
SNAP_S = 6 * 3600
SF = Path(__file__).resolve().parents[1] / "bot_status.json"
PF = Path(__file__).resolve().parents[1] / "profit_btc.json"


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
            cut = datetime.now(timezone.utc) - timedelta(minutes=WS_WIN)
            self.dc = [t for t in self.dc if t > cut]

    def rec(self):
        self.dc.append(datetime.now(timezone.utc))
        [logger.critical("WS UNSTABLE"), self.c.stop()] if len(
            self.dc
        ) >= MAX_WS else None


class BCtrl:
    def __init__(self):
        self.run = False
        self.risk = RiskManager()
        self.exec = TradeExecutor()
        self.wd = Nwd(self)
        self._twm = None
        self._start = None
        self._trades = 0
        self._pbtc = 0.0
        self._candles = {}
        self._prices = {}
        self._lsnap = datetime.min.replace(tzinfo=timezone.utc)
        self._lcfg = datetime.min.replace(tzinfo=timezone.utc)
        for s in get_config().bot.symbols:
            self._candles[s] = deque(maxlen=MAX_C)

    def _lprofit(self):
        if PF.exists():
            try:
                self._pbtc = json.loads(PF.read_text()).get("total_btc", 0.0)
            except:
                pass

    def _sprofit(self):
        PF.write_text(
            json.dumps(
                {
                    "total_btc": round(self._pbtc, 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fix(self, p):
        bp = get_current_price("BTCUSDT")
        if bp <= 0:
            return
        btc = p / bp
        self._pbtc += btc
        self._sprofit()
        logger.info(f"PROFIT: {p:.2f} USDT → {btc:.8f} BTC (total: {self._pbtc:.8f})")
        s = get_session()
        s.add(
            BotLog(
                level="INFO",
                category="profit",
                message=f"+{p:.2f} USDT → {btc:.8f} BTC | Total: {self._pbtc:.8f} BTC",
            )
        )
        s.commit()

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
                    balances_json={"profit_btc": self._pbtc},
                )
            )
            s.commit()
            self._lsnap = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {t:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot: {e}")

    def _scandle(self, sym, k, c):
        try:
            s = get_session()
            ot = datetime.fromtimestamp(k.get("t", 0) / 1000, tz=timezone.utc)
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
                        close_time=datetime.fromtimestamp(
                            k.get("T", 0) / 1000, tz=timezone.utc
                        ),
                        open=c["open"],
                        high=c["high"],
                        low=c["low"],
                        close=c["close"],
                        volume=c["volume"],
                        trades_count=k.get("n", 0),
                    )
                )
                s.commit()
        except:
            pass

    def _wstatus(self):
        STATUS_FILE.write_text(
            json.dumps(
                {
                    "running": self.run,
                    "started_at": self._start.isoformat() if self._start else None,
                    "network_ok": ping(),
                    "ws_dc": len(self.wd.dc),
                    "trades": self._trades,
                    "profit_btc": round(self._pbtc, 8),
                    **(self.risk.get_daily_stats()),
                },
                indent=2,
            )
        )

    def start(self):
        if self.run:
            return
        if not ping():
            print("ERROR: Binance unreachable")
            return
        self.run = True
        self._start = datetime.now(timezone.utc)
        self._lprofit()
        self.wd.start()
        self._wstatus()
        self.risk.day_start_balance = get_account_balance("USDT")
        logger.info("=== Trading Bot STARTED ===")
        self._lcandles()
        self._snap()
        self._wstart()

    def stop(self):
        if not self.run:
            return
        self.run = False
        self.wd.stop()
        if self._twm:
            try:
                self._twm.stop()
            except:
                pass
            self._twm = None
        self._snap()
        self._wstatus()
        logger.info("=== STOPPED ===")

    def _lcandles(self):
        cl = get_client()
        for sym, buf in self._candles.items():
            try:
                for k in cl.get_klines(symbol=sym, interval=INTERVAL, limit=50):
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
                logger.info(f"Loaded {len(buf)} candles {sym}")
            except Exception as e:
                logger.error(f"Candles {sym}: {e}")

    def mclose(self, sym):
        s = get_session()
        pos = s.query(PM).filter(PM.symbol == sym, PM.status == "OPEN").first()
        if not pos:
            print(f"No position: {sym}")
            return False
        return self.exec.close_position(pos, get_current_price(sym), reason="manual")

    def sstatus(self):
        d = self.risk.get_daily_stats()
        s = get_session()
        pos = s.query(PM).filter(PM.status == "OPEN").all()
        run = False
        pbtc = 0.0
        if STATUS_FILE.exists():
            try:
                j = json.loads(STATUS_FILE.read_text())
                run = j.get("running", False)
                pbtc = j.get("profit_btc", 0.0)
            except:
                pass
        bal = get_account_balance("USDT")
        print(f"\n{'=' * 45}\n  Trading Bot Status ({INTERVAL})\n{'=' * 45}")
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

    def slogs(self, n=20):
        for e in reversed(
            get_session()
            .query(BotLog)
            .order_by(BotLog.created_at.desc())
            .limit(n)
            .all()
        ):
            print(f"[{e.level}] [{e.category or '-'}] {e.message}")

    def _wstart(self):
        cfg = get_config()
        try:
            from binance import ThreadedWebsocketManager

            self._twm = ThreadedWebsocketManager(
                api_key=cfg.binance.api_key,
                api_secret=cfg.binance.api_secret,
                testnet=True,
            )
            self._twm.start()
            self._twm.start_multiplex_socket(
                callback=self._k,
                streams=[f"{s.lower()}@kline_{INTERVAL}" for s in cfg.bot.symbols],
            )
            logger.info(f"WebSocket {INTERVAL}: {cfg.bot.symbols}")
            self._twm.join()
        except Exception as e:
            logger.error(f"WebSocket: {e}")
            self.wd.rec()

    def _k(self, msg):
        if not self.run:
            return
        d = msg.get("data", {})
        k = d.get("k", {})
        sym = k.get("s", "")
        if sym not in self._candles:
            return
        closed = k.get("x", False)
        c = {
            "open": float(k.get("o", 0)),
            "high": float(k.get("h", 0)),
            "low": float(k.get("l", 0)),
            "close": float(k.get("c", 0)),
            "volume": float(k.get("v", 0)),
        }
        self._prices[sym] = c["close"]
        buf = self._candles[sym]
        if closed:
            if buf and buf[-1]["close"] == c["close"] and buf[-1]["open"] == c["open"]:
                buf[-1] = c
            else:
                buf.append(c)
            self._scandle(sym, k, c)
        else:
            if buf:
                buf[-1] = c
            else:
                buf.append(c)
        if not closed or len(buf) < 25:
            return
        cfg = get_config()
        # Drawdown
        if self.risk.day_start_balance > 0:
            self.risk.check_daily_drawdown(get_account_balance("USDT"))
        # Config reload every 5 min
        if (datetime.now(timezone.utc) - self._lcfg).total_seconds() > 300:
            reload_from_db()
            self._lcfg = datetime.now(timezone.utc)
        # Breaker
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / c["close"]) * 100 > 3.0:
                self.risk.check_atr_volatility((atr / c["close"]) * 100)
        if len(buf) >= 12:
            self.risk.check_price_crash(sym, c["close"], list(buf)[-12]["close"])
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
                self.exec.close_position(pos, price, "take_profit")
                self.risk.record_trade(pnl)
                self._trades += 1
                logger.info(f"TP {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix(pnl)
                    return
            if pos.stop_loss and price <= float(pos.stop_loss):
                pnl = float(pos.unrealized_pnl)
                self.exec.close_position(pos, price, "stop_loss")
                self.risk.record_trade(pnl)
                self._trades += 1
                logger.info(f"SL {sym} @ {price:.2f} PnL={pnl:.2f}")
                if pnl > 0:
                    self._fix(pnl)
                    return
        else:
            df = pd.DataFrame(list(buf))
            signal = BollingerRSIStrategy(
                symbol=sym, interval=INTERVAL, use_strict_filter=cfg.bot.use_strict_rsi
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
        if (datetime.now(timezone.utc) - self._lsnap).total_seconds() > SNAP_S:
            self._snap()


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
        logger.error("Cannot connect")
        sys.exit(1)
    logger.info("Network OK")
    ctrl = BCtrl()
    if cmd == "start":
        ctrl.start()
        try:
            while ctrl.run:
                time.sleep(1)
        except KeyboardInterrupt:
            ctrl.stop()
    elif cmd == "stop":
        ctrl.stop()
    else:
        print("Commands: start | stop | status | close SYMBOL | logs N")


if __name__ == "__main__":
    main()
