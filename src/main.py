"""Trading Bot — REST 1m. OCO SL/TP on exchange. Profit: BTC→BTC, ETH→USDT."""

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
from src.binance_client import *
from src.config import get_config
from src.database import get_session, init_db
from src.logger import setup_logging
from src.models import AccountSnapshot, BotLog, MarketData
from src.models import Position as PM
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy
from src.trade_executor import TradeExecutor

I = "1m"
PS = 15
PING_S = 30
MPF = 3
MC = 100
PCC = 12
SS = 21600
SF = Path(__file__).resolve().parents[1] / "bot_status.json"
PF = Path(__file__).resolve().parents[1] / "profit_state.json"


def _bb_cols(df):
    bl = bm = None
    for c in df.columns:
        if c.startswith("BBL_"):
            bl = c
        elif c.startswith("BBM_"):
            bm = c
    return bl, bm


class Nwd:
    def __init__(self, c):
        self.c = c
        self.e = threading.Event()
        self.pf = 0

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
                logger.warning(f"Ping fail {self.pf}/{MPF}")
            if self.pf >= MPF:
                logger.critical("NET LOST")
                self.c.stop()


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
        self.candles: dict[str, deque] = {}
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.lsnap = datetime.min.replace(tzinfo=timezone.utc)
        self.lstatus: dict[str, datetime] = {}
        self.lct: dict[str, int] = {}
        for s in get_config().bot.symbols:
            self.candles[s] = deque(maxlen=MC)
            self.strategies[s] = BollingerRSIStrategy(symbol=s, interval=I)
            self.lstatus[s] = datetime.min.replace(tzinfo=timezone.utc)
        self._lp()

    def _lp(self):
        if PF.exists():
            try:
                d = json.loads(PF.read_text())
                self.pbtc = d.get("total_btc", 0)
                self.pusdt = d.get("total_usdt", 0)
            except:
                pass

    def _sp(self):
        PF.write_text(
            json.dumps(
                {
                    "total_btc": round(self.pbtc, 8),
                    "total_usdt": round(self.pusdt, 4),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def _fp(self, pnl, sym):
        if pnl <= 0:
            return
        if "BTC" in sym:
            if pnl < 15:
                logger.info(f"BTC profit {pnl:.2f} accumulating")
                return
            o = place_market_buy("BTCUSDT", pnl)
            if not o or not o.get("fills"):
                logger.error("Buy BTC failed")
                return
            btc = sum(float(f["qty"]) for f in o["fills"])
            transfer_to_funding("BTC", btc)
            self.pbtc += btc
            logger.info(f"PROFIT: {pnl:.2f}USDT->{btc:.8f}BTC | Total:{self.pbtc:.8f}")
        else:
            transfer_to_funding("USDT", pnl)
            self.pusdt += pnl
            logger.info(f"PROFIT: {pnl:.2f}USDT->Funding | Total:{self.pusdt:.2f}")
        self._sp()

    def _snap(self):
        try:
            b = get_account_balance("USDT")
            s = get_session()
            s.add(
                AccountSnapshot(
                    total_balance=b,
                    available_balance=b,
                    locked_balance=0,
                    snapshot_time=datetime.now(timezone.utc),
                    balances_json={"pbtc": self.pbtc, "pusdt": self.pusdt},
                )
            )
            s.commit()
            s.close()
            self.lsnap = datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"Snap: {e}")

    def _ws(self):
        SF.write_text(
            json.dumps(
                {
                    "running": self.running,
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
            print("ERR")
            return
        self.running = True
        self.started_at = datetime.now(timezone.utc)
        self.wd.start()
        self.risk.day_start_balance = get_account_balance("USDT")
        self._ws()
        self._lc()
        self._snap()
        logger.info("=== STARTED ===")
        self._poll()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.wd.stop()
        self._snap()
        self._ws()
        logger.info("=== STOPPED ===")

    def _lc(self):
        cl = get_client()
        for s in self.candles:
            try:
                ks = cl.get_klines(symbol=s, interval=I, limit=50)
                for k in ks:
                    self.candles[s].append(
                        {
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                            "ot": k[0],
                            "ct": k[6],
                        }
                    )
                self.lct[s] = ks[-1][6] if ks else 0
                logger.info(f"Loaded {len(ks)} {s}")
            except Exception as e:
                logger.error(f"Load {s}: {e}")

    def _poll(self):
        cl = get_client()
        while self.running:
            for s in list(self.candles.keys()):
                try:
                    ks = cl.get_klines(symbol=s, interval=I, limit=3)
                    for k in ks:
                        ct = k[6]
                        if ct <= self.lct.get(s, 0):
                            continue
                        self.lct[s] = ct
                        self._ok(
                            {
                                "k": {
                                    "t": k[0],
                                    "T": k[6],
                                    "s": s,
                                    "open": k[1],
                                    "high": k[2],
                                    "low": k[3],
                                    "close": k[4],
                                    "volume": k[5],
                                    "n": k[8],
                                    "x": True,
                                }
                            }
                        )
                except Exception as e:
                    logger.error(f"Poll {s}: {e}")
            time.sleep(PS)

    def _ok(self, msg):
        try:
            k = msg["k"]
            sym = k["s"]
            c = {
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": float(k["volume"]),
            }
            buf = self.candles[sym]
            if buf and buf[-1].get("ot") == k["t"]:
                buf[-1] = c
            else:
                buf.append(c)
            if len(buf) < 25:
                return
            # Verbose every 60s per symbol
            now = datetime.now(timezone.utc)
            if (
                now - self.lstatus.get(sym, datetime.min.replace(tzinfo=timezone.utc))
            ).total_seconds() >= 60:
                self.lstatus[sym] = now
                df = pd.DataFrame(list(buf))
                try:
                    import pandas_ta as ta

                    cs = df["close"]
                    bb = ta.bbands(cs, length=20, std=2)
                    rs = ta.rsi(cs, length=14)
                    blc, bmc = _bb_cols(bb) if bb is not None else (None, None)
                    lo = float(bb.iloc[-1][blc]) if blc else 0
                    mi = float(bb.iloc[-1][bmc]) if bmc else 0
                    rv = (
                        float(rs.iloc[-1])
                        if rs is not None and not pd.isna(rs.iloc[-1])
                        else 0
                    )
                    sig = "BUY" if lo and c["close"] <= lo and rv < 35 else "HOLD"
                    ps = ""
                    s = get_session()
                    p = (
                        s.query(PM)
                        .filter(PM.symbol == sym, PM.status == "OPEN")
                        .first()
                    )
                    if p:
                        ps = f" | POS: ent={float(p.entry_price):.2f} SL={float(p.stop_loss or 0):.2f} TP={float(p.take_profit or 0):.2f}"
                    s.close()
                    logger.info(
                        f"[{sym}] {c['close']:.2f} | BB:{lo:.2f}/{mi:.2f} | RSI:{rv:.1f} | {sig}{ps}"
                    )
                except Exception as ex:
                    logger.info(f"[{sym}] {c['close']:.2f} | ind fail: {ex}")
            # Save to DB
            try:
                s = get_session()
                ot = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc)
                if (
                    not s.query(MarketData)
                    .filter(
                        MarketData.symbol == sym,
                        MarketData.interval == I,
                        MarketData.open_time == ot,
                    )
                    .first()
                ):
                    s.add(
                        MarketData(
                            symbol=sym,
                            interval=I,
                            open_time=ot,
                            close_time=datetime.fromtimestamp(
                                k["T"] / 1000, tz=timezone.utc
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
                s.close()
            except Exception:
                pass
            # --- Circuit Breaker with new thresholds ---
            if self.risk.day_start_balance > 0:
                self.risk.check_daily_drawdown(get_account_balance("USDT"))
            if len(buf) >= 15:
                df = pd.DataFrame(list(buf))
                atr = (df["high"] - df["low"]).tail(14).mean()
                if (atr / c["close"]) * 100 > 3.0:
                    self.risk.check_atr_volatility((atr / c["close"]) * 100)
            # -5% in 1h → FULL STOP (RED)
            if len(buf) >= 60:
                self.risk.check_price_crash_red(
                    sym, c["close"], list(buf)[-60]["close"]
                )
            # -2.5% in 1h → pause 60min (ORANGE)
            if len(buf) >= 60:
                self.risk.check_price_decline_orange(
                    sym, c["close"], list(buf)[-60]["close"]
                )
            # If RED breaker: stop bot completely
            if self.risk.breaker_level.value == "RED":
                logger.critical("RED BREAKER — full stop")
                self.stop()
            if not self.risk.is_trading_allowed()[0]:
                return
            # --- Sync positions: check if OCO closed position ---
            s = get_session()
            pos = s.query(PM).filter(PM.symbol == sym, PM.status == "OPEN").first()
            if pos:
                # Check if OCO already sold (balance = 0)
                asset = sym.replace("USDT", "")
                bal = get_account_balance(asset)
                if bal < float(pos.quantity) * 0.5 and float(pos.quantity) > 0:
                    # OCO executed! Close in DB
                    pr = c["close"]
                    q = float(pos.quantity)
                    pnl = (pr - float(pos.entry_price)) * q
                    pos.current_price = pr
                    pos.realized_pnl = pnl
                    pos.status = "CLOSED"
                    pos.closed_at = datetime.now(timezone.utc)
                    s.commit()
                    self.risk.record_trade(pnl)
                    self.trades += 1
                    logger.info(f"OCO closed {sym} @{pr:.2f} PnL={pnl:.2f}")
                    s.close()
                    if pnl > 0:
                        self._fp(pnl, sym)
                else:
                    # Update unrealized PnL
                    pos.current_price = c["close"]
                    pos.unrealized_pnl = (c["close"] - float(pos.entry_price)) * float(
                        pos.quantity
                    )
                    s.commit()
                    s.close()
            else:
                s.close()
                # No position — check entry signal
                df = pd.DataFrame(list(buf))
                sg = self.strategies[sym].analyze(df)
                if sg.signal.value == "BUY":
                    logger.info(f">>BUY {sym} @{sg.price:.2f} ({sg.reason})")
                    if self.exec.open_position(
                        sym, sg.price, sg.stop_loss, sg.take_profit
                    ):
                        self.trades += 1
            self._ws()
            if (datetime.now(timezone.utc) - self.lsnap).total_seconds() > SS:
                self._snap()
        except Exception as e:
            logger.error(f"_ok {msg.get('k', {}).get('s', '?')}: {e}")

    def mc(self, sym):
        s = get_session()
        pos = s.query(PM).filter(PM.symbol == sym, PM.status == "OPEN").first()
        if not pos:
            s.close()
            print(f"No pos: {sym}")
            return False
        s.close()
        return self.exec.close_position(pos, get_current_price(sym), reason="manual")

    def st(self):
        d = self.risk.get_daily_stats()
        s = get_session()
        pos = s.query(PM).filter(PM.status == "OPEN").all()
        s.close()
        r = False
        pb = 0.0
        pu = 0.0
        if SF.exists():
            try:
                j = json.loads(SF.read_text())
                r = j.get("running", False)
                pb = j.get("profit_btc", 0)
                pu = j.get("profit_usdt", 0)
            except:
                pass
        print(f"\n{'=' * 50}\n  Bot ({I})\n{'=' * 50}")
        print(f"  Running: {r} | Net: {'OK' if ping() else 'FAIL'}")
        print(f"  Balance: {get_account_balance('USDT'):.2f} USDT")
        print(f"  Profit: {pb:.8f} BTC | {pu:.2f} USDT")
        print(
            f"  Trades: {d['trades']} | PnL: {d['pnl']} USDT | Breaker: {d['breaker']}"
        )
        if pos:
            print(f"\n  Positions ({len(pos)}):")
            for p in pos:
                print(
                    f"    {p.symbol} ent={float(p.entry_price):.2f} q={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
                )
        else:
            print("\n  Positions: 0")
        print(f"{'=' * 50}\n")

    def sl(self, n=20):
        s = get_session()
        ls = s.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
        s.close()
        for e in reversed(ls):
            print(f"[{e.level}] [{e.category or '-'}] {e.message}")


def main():
    setup_logging()
    init_db()
    a = sys.argv[1].lower() if len(sys.argv) >= 2 else ""
    if a == "status":
        BCtrl().st()
        return
    if a == "close" and len(sys.argv) >= 3:
        BCtrl().mc(sys.argv[2].upper())
        return
    if a == "logs":
        BCtrl().sl(int(sys.argv[2]) if len(sys.argv) >= 3 else 20)
        return
    if not ping():
        logger.error("Binance unreachable")
        sys.exit(1)
    logger.info("Network OK")
    c = BCtrl()
    if a == "start":
        c.start()
        try:
            while c.running:
                time.sleep(1)
        except KeyboardInterrupt:
            c.stop()
    elif a == "stop":
        c.stop()
    else:
        print("start|stop|status|close SYMBOL|logs N")


if __name__ == "__main__":
    main()
