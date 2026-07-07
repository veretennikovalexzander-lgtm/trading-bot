"""WebSocket market data streamer — async version."""
from __future__ import annotations
import asyncio, json
from datetime import datetime, timezone
import pandas as pd
from loguru import logger
from src.binance_client import get_account_balance, get_client, get_current_price
from src.config import get_config
from src.database import get_session
from src.models import MarketData, Position as PositionModel
from src.controller import BotController

INTERVAL = "1m"
PRICE_CRASH_CANDLES = 60
SNAP_INTERVAL_SEC = 6 * 3600
RECONNECT_DELAY = 3
MAX_RECONNECT_DELAY = 120


def _find_bb_columns(df):
    lc = mc = None
    for c in df.columns:
        if c.startswith("BBL_"): lc = c
        elif c.startswith("BBM_"): mc = c
    return lc, mc


class WebSocketStreamer:
    def __init__(self, controller, profit_manager, on_position_closed):
        self.controller = controller
        self.profit_manager = profit_manager
        self._on_position_closed = on_position_closed
        self._last_status_log: dict[str, datetime] = {}
        self._last_snapshot = datetime.min.replace(tzinfo=timezone.utc)
        for s in controller.strategies:
            self._last_status_log[s] = datetime.min.replace(tzinfo=timezone.utc)

    def load_history(self):
        cl = get_client()
        for sym in self.controller.candles:
            try:
                ks = cl.get_klines(symbol=sym, interval=INTERVAL, limit=50)
                for k in ks:
                    self.controller.candles[sym].append({
                        "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5]), "ot": k[0], "ct": k[6],
                    })
                self.controller.last_close_times[sym] = ks[-1][6] if ks else 0
                logger.info(f"Loaded {len(ks)} candles: {sym}")
            except Exception as e:
                logger.error(f"Load {sym}: {e}")

    def sync_positions(self):
        """Close DB positions whose assets were sold by OCO while bot was offline."""
        s = get_session()
        for p in s.query(PositionModel).filter(PositionModel.status == "OPEN").all():
            asset = p.symbol.replace("USDT", "")
            bal = get_account_balance(asset)
            if bal < float(p.quantity) * 0.5:
                px = get_current_price(p.symbol)
                pnl = (px - float(p.entry_price)) * float(p.quantity)
                p.current_price = px
                p.realized_pnl = pnl
                p.status = "CLOSED"
                p.closed_at = datetime.now(timezone.utc)
                logger.info(f"Sync-closed {p.symbol}: OCO offline. PnL={pnl:.2f}")
                self.controller.risk_manager.record_trade(pnl)
                self.controller.trades_count += 1
                if pnl > 0:
                    self.profit_manager.fix(pnl, p.symbol)
        s.commit()
        s.close()

    async def run(self):
        cfg = get_config()
        streams = "/".join(f"{s.lower()}@kline_{INTERVAL}" for s in cfg.bot.symbols)
        ws_url = f"wss://stream.binance.com:9443/ws/{streams}"
        backoff = RECONNECT_DELAY
        while self.controller.running:
            try:
                import websockets
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info(f"WS connected ({len(cfg.bot.symbols)} streams)")
                    backoff = RECONNECT_DELAY
                    async for raw in ws:
                        if not self.controller.running: break
                        try:
                            self._handle_message(raw)
                        except Exception as e:
                            logger.error(f"Msg err: {e}")
            except Exception as e:
                logger.error(f"WS err: {e}")
            if not self.controller.running: break
            logger.warning(f"Reconnecting {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

    def _handle_message(self, raw):
        msg = json.loads(raw)
        kline = msg["data"]["k"] if "data" in msg else msg.get("k", {})
        if not kline or not kline.get("x"): return
        sym = kline.get("s", "")
        if sym not in self.controller.strategies: return
        ct = kline.get("T", 0)
        if ct <= self.controller.last_close_times.get(sym, 0): return
        self.controller.last_close_times[sym] = ct
        self._process_candle(sym, kline)

    def _process_candle(self, sym, k):
        c = {"open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
             "close": float(k["c"]), "volume": float(k["v"])}
        buf = self.controller.candles[sym]
        if buf and buf[-1].get("ot") == k["t"]: buf[-1] = c
        else: buf.append(c)
        if len(buf) < 25: return
        px = c["close"]
        # Status every 60s
        now = datetime.now(timezone.utc)
        if (now - self._last_status_log.get(sym, datetime.min.replace(tzinfo=timezone.utc))).total_seconds() >= 60:
            self._last_status_log[sym] = now
            self._log_status(sym, buf, px)
        self._save_candle(sym, k, c)
        # Breaker
        ctrl = self.controller
        if ctrl.risk_manager.day_start_balance > 0:
            ctrl.risk_manager.check_daily_drawdown(get_account_balance("USDT"))
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / px) * 100 > 3.0:
                ctrl.risk_manager.check_atr_volatility((atr / px) * 100)
        if len(buf) >= PRICE_CRASH_CANDLES:
            p1h = list(buf)[-PRICE_CRASH_CANDLES]["close"]
            ctrl.risk_manager.check_price_crash_red(sym, px, p1h)
            ctrl.risk_manager.check_price_decline_orange(sym, px, p1h)
        if ctrl.risk_manager.breaker_level.value == "RED":
            logger.critical("RED BREAKER")
            self.controller.running = False; return
        if not ctrl.risk_manager.is_trading_allowed()[0]: return
        # Position
        s = get_session()
        pos = s.query(PositionModel).filter(PositionModel.symbol == sym, PositionModel.status == "OPEN").first()
        if pos:
            asset = sym.replace("USDT", "")
            bal = get_account_balance(asset)
            if bal < float(pos.quantity) * 0.5 and float(pos.quantity) > 0:
                pnl = (px - float(pos.entry_price)) * float(pos.quantity)
                pos.current_price = px; pos.realized_pnl = pnl
                pos.status = "CLOSED"; pos.closed_at = datetime.now(timezone.utc)
                s.commit()
                ctrl.risk_manager.record_trade(pnl); ctrl.trades_count += 1
                logger.info(f"OCO closed {sym} @{px:.2f} PnL={pnl:.2f}")
                s.close()
                if pnl > 0: self.profit_manager.fix(pnl, sym)
                self._on_position_closed(pnl)
            else:
                pos.current_price = px
                pos.unrealized_pnl = (px - float(pos.entry_price)) * float(pos.quantity)
                s.commit(); s.close()
        else:
            s.close()
            df = pd.DataFrame(list(buf))
            sg = self.controller.strategies[sym].analyze(df)
            if sg.signal.value == "BUY":
                logger.info(f">>BUY {sym} @{sg.price:.2f} ({sg.reason})")
                if self.controller.executor.open_position(sym, sg.price, sg.stop_loss, sg.take_profit):
                    ctrl.trades_count += 1
        ctrl.write_status()
        if (datetime.now(timezone.utc) - self._last_snapshot).total_seconds() > SNAP_INTERVAL_SEC:
            self._take_snapshot()

    def _log_status(self, sym, buf, px):
        try:
            import pandas_ta as ta
            df = pd.DataFrame(list(buf))
            bb = ta.bbands(df["close"], length=20, std=2)
            rs = ta.rsi(df["close"], length=14)
            lc, mc = _find_bb_columns(bb) if bb is not None else (None, None)
            lo = float(bb.iloc[-1][lc]) if lc else 0
            mi = float(bb.iloc[-1][mc]) if mc else 0
            rv = float(rs.iloc[-1]) if rs is not None and not pd.isna(rs.iloc[-1]) else 0
            s = get_session()
            p = s.query(PositionModel).filter(PositionModel.symbol == sym, PositionModel.status == "OPEN").first()
            if p:
                sig = "POSITION"
                ps = f" | POS: ent={float(p.entry_price):.2f} SL={float(p.stop_loss or 0):.2f} TP={float(p.take_profit or 0):.2f}"
            else:
                sig = "BUY" if lo and px <= lo and rv < get_config().bot.rsi_threshold else "HOLD"
                ps = ""
            s.close()
            logger.info(f"[{sym}] {px:.2f} | BB:{lo:.2f}/{mi:.2f} | RSI:{rv:.1f} | {sig}{ps}")
        except Exception as e:
            logger.error(f"[{sym}] ind err: {e}")

    def _save_candle(self, sym, k, c):
        try:
            s = get_session()
            ot = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc)
            if not s.query(MarketData).filter(MarketData.symbol == sym, MarketData.interval == INTERVAL, MarketData.open_time == ot).first():
                s.add(MarketData(symbol=sym, interval=INTERVAL, open_time=ot, close_time=datetime.fromtimestamp(k["T"] / 1000, tz=timezone.utc), open=c["open"], high=c["high"], low=c["low"], close=c["close"], volume=c["volume"], trades_count=k.get("n", 0)))
                s.commit()
            s.close()
        except Exception: pass

    def _take_snapshot(self):
        try:
            from src.models import AccountSnapshot
            bal = get_account_balance("USDT")
            s = get_session()
            s.add(AccountSnapshot(total_balance=bal, available_balance=bal, locked_balance=0, snapshot_time=datetime.now(timezone.utc), balances_json={"pbtc": self.profit_manager.total_btc, "pusdt": self.profit_manager.total_usdt}))
            s.commit(); s.close()
            self._last_snapshot = datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"Snap: {e}")
