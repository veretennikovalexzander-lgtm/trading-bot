"""WebSocket market data streamer — async version using websockets library."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pandas as pd
from loguru import logger
from src.binance_client import get_account_balance, get_client
from src.config import get_config
from src.controller import BotController
from src.database import get_session
from src.models import MarketData
from src.models import Position as PositionModel

INTERVAL = "1m"
PRICE_CRASH_CANDLES = 60
SNAP_INTERVAL_SEC = 6 * 3600
RECONNECT_DELAY = 3
MAX_RECONNECT_DELAY = 120

_ws_lock = asyncio.Lock()


def _find_bb_columns(dataframe):
    lower_col = mid_col = None
    for col in dataframe.columns:
        if col.startswith("BBL_"):
            lower_col = col
        elif col.startswith("BBM_"):
            mid_col = col
    return lower_col, mid_col


class WebSocketStreamer:
    """Async WebSocket kline streamer with exponential reconnect backoff."""

    def __init__(self, controller: BotController, profit_manager, on_position_closed):
        self.controller = controller
        self.profit_manager = profit_manager
        self._on_position_closed = on_position_closed
        self._last_status_log: dict[str, datetime] = {}
        self._last_snapshot = datetime.min.replace(tzinfo=timezone.utc)
        for symbol in controller.strategies:
            self._last_status_log[symbol] = datetime.min.replace(tzinfo=timezone.utc)

    def load_history(self):
        """Prime candle buffers with 50 historical candles (sync, called before async loop)."""
        client = get_client()
        for symbol in self.controller.candles:
            try:
                klines = client.get_klines(symbol=symbol, interval=INTERVAL, limit=50)
                for k in klines:
                    self.controller.candles[symbol].append(
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
                self.controller.last_close_times[symbol] = (
                    klines[-1][6] if klines else 0
                )
                logger.info(f"Loaded {len(klines)} candles: {symbol}")
            except Exception as e:
                logger.error(f"Load candles {symbol}: {e}")

    async def run(self):
        """Async main loop: connect to Binance WebSocket with reconnect."""
        cfg = get_config()
        streams = "/".join(f"{s.lower()}@kline_{INTERVAL}" for s in cfg.bot.symbols)
        base_url = (
            "wss://stream.binance.com:9443/ws"
            if cfg.binance.testnet
            else "wss://ws.binance.com:9443/ws"
        )
        ws_url = f"{base_url}/{streams}"
        logger.info(f"WebSocket URL: {ws_url}")

        backoff = RECONNECT_DELAY

        while self.controller.running:
            try:
                import websockets

                async with websockets.connect(
                    ws_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.info(f"WebSocket connected ({len(cfg.bot.symbols)} streams)")
                    backoff = RECONNECT_DELAY
                    async for raw in ws:
                        if not self.controller.running:
                            break
                        try:
                            await self._handle_message(raw)
                        except Exception as e:
                            logger.error(f"Message handler error: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if not self.controller.running:
                break

            logger.warning(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

    async def _handle_message(self, raw: str):
        if not hasattr(self, "_msg_count"): self._msg_count = 0
        self._msg_count += 1
        if self._msg_count % 30 == 0:
            from loguru import logger
            logger.debug(f"WebSocket messages received: {self._msg_count}")
        msg = json.loads(raw)
        if self._msg_count == 1: logger.info(f"First WS message keys: {list(msg.keys())}")
        if "data" in msg:
            kline = msg["data"].get("k", {})
        else:
            kline = msg.get("k", {})
        if not kline or not kline.get("x", False):  # Only closed candles
            if self._msg_count % 60 == 0: logger.debug(f"Non-closed kline")
            return

        symbol = kline.get("s", "")
        if symbol not in self.controller.strategies:
            return

        close_time = kline.get("T", 0) // 1000
        if close_time <= self.controller.last_close_times.get(symbol, 0):
            return
        self.controller.last_close_times[symbol] = close_time

        # Process candle in thread pool (pandas/DB are blocking)
        await asyncio.to_thread(self._process_candle, symbol, kline)

    def _process_candle(self, symbol: str, kline: dict):
        """Sync processing: update buffer, indicators, trade logic."""
        candle = {
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
        }
        buf = self.controller.candles[symbol]
        if buf and buf[-1].get("ot") == kline["t"]:
            buf[-1] = candle
        else:
            buf.append(candle)
        if len(buf) < 25:
            return

        close_price = candle["close"]

        # Status log (every 60s per symbol)
        now = datetime.now(timezone.utc)
        if (
            now
            - self._last_status_log.get(
                symbol, datetime.min.replace(tzinfo=timezone.utc)
            )
        ).total_seconds() >= 60:
            self._last_status_log[symbol] = now
            self._log_market_status(symbol, buf, close_price)

        # Save candle
        self._save_candle(symbol, kline, candle)

        # Breaker checks
        ctrl = self.controller
        if ctrl.risk_manager.day_start_balance > 0:
            ctrl.risk_manager.check_daily_drawdown(get_account_balance("USDT"))
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / close_price) * 100 > 3.0:
                ctrl.risk_manager.check_atr_volatility((atr / close_price) * 100)
        if len(buf) >= PRICE_CRASH_CANDLES:
            price_1h_ago = list(buf)[-PRICE_CRASH_CANDLES]["close"]
            ctrl.risk_manager.check_price_crash_red(symbol, close_price, price_1h_ago)
            ctrl.risk_manager.check_price_decline_orange(
                symbol, close_price, price_1h_ago
            )
        if ctrl.risk_manager.breaker_level.value == "RED":
            logger.critical("RED BREAKER — stopping")
            self.controller.running = False
            return
        if not ctrl.risk_manager.is_trading_allowed()[0]:
            return

        # Position management
        session = get_session()
        position = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )
        if position:
            asset = symbol.replace("USDT", "")
            balance = get_account_balance(asset)
            if (
                balance < float(position.quantity) * 0.5
                and float(position.quantity) > 0
            ):
                qty = float(position.quantity)
                pnl = (close_price - float(position.entry_price)) * qty
                position.current_price = close_price
                position.realized_pnl = pnl
                position.status = "CLOSED"
                position.closed_at = datetime.now(timezone.utc)
                session.commit()
                ctrl.risk_manager.record_trade(pnl)
                ctrl.trades_count += 1
                logger.info(f"OCO closed {symbol} @ {close_price:.2f} PnL={pnl:.2f}")
                session.close()
                if pnl > 0:
                    self.profit_manager.fix(pnl, symbol)
                self._on_position_closed(pnl)
            else:
                position.current_price = close_price
                position.unrealized_pnl = (
                    close_price - float(position.entry_price)
                ) * float(position.quantity)
                session.commit()
                session.close()
        else:
            session.close()
            df = pd.DataFrame(list(buf))
            signal = self.controller.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                logger.info(f">> BUY {symbol} @ {signal.price:.2f} ({signal.reason})")
                if self.controller.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                ):
                    ctrl.trades_count += 1

        ctrl.write_status()

        if (
            datetime.now(timezone.utc) - self._last_snapshot
        ).total_seconds() > SNAP_INTERVAL_SEC:
            self._take_snapshot()

    def _log_market_status(self, symbol: str, buf, close_price: float):
        try:
            import pandas_ta as ta

            df = pd.DataFrame(list(buf))
            bb = ta.bbands(df["close"], length=20, std=2)
            rsi_series = ta.rsi(df["close"], length=14)
            lc, mc = _find_bb_columns(bb) if bb is not None else (None, None)
            lo = float(bb.iloc[-1][lc]) if lc else 0
            mi = float(bb.iloc[-1][mc]) if mc else 0
            rv = (
                float(rsi_series.iloc[-1])
                if rsi_series is not None and not pd.isna(rsi_series.iloc[-1])
                else 0
            )
            sig = "BUY" if lo and close_price <= lo and rv < 40 else "HOLD"
            ps = ""
            session = get_session()
            p = (
                session.query(PositionModel)
                .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
                .first()
            )
            if p:
                ps = f" | POS: ent={float(p.entry_price):.2f} SL={float(p.stop_loss or 0):.2f} TP={float(p.take_profit or 0):.2f}"
            session.close()
            logger.info(
                f"[{symbol}] {close_price:.2f} | BB:{lo:.2f}/{mi:.2f} | RSI:{rv:.1f} | {sig}{ps}"
            )
        except Exception as e:
            logger.info(f"[{symbol}] {close_price:.2f} | ind err: {e}")

    def _save_candle(self, symbol: str, kline: dict, candle: dict):
        try:
            session = get_session()
            ot = datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc)
            if (
                not session.query(MarketData)
                .filter(
                    MarketData.symbol == symbol,
                    MarketData.interval == INTERVAL,
                    MarketData.open_time == ot,
                )
                .first()
            ):
                session.add(
                    MarketData(
                        symbol=symbol,
                        interval=INTERVAL,
                        open_time=ot,
                        close_time=datetime.fromtimestamp(
                            kline["T"] / 1000, tz=timezone.utc
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

    def _take_snapshot(self):
        try:
            from src.models import AccountSnapshot

            balance = get_account_balance("USDT")
            session = get_session()
            session.add(
                AccountSnapshot(
                    total_balance=balance,
                    available_balance=balance,
                    locked_balance=0,
                    snapshot_time=datetime.now(timezone.utc),
                    balances_json={
                        "profit_btc": self.profit_manager.total_btc,
                        "profit_usdt": self.profit_manager.total_usdt,
                    },
                )
            )
            session.commit()
            session.close()
            self._last_snapshot = datetime.now(timezone.utc)
            logger.info(f"Snapshot: {balance:.2f} USDT")
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
