"""Market data poller: fetches klines, processes candles, triggers strategy."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

import pandas as pd
from loguru import logger
from src.binance_client import (
    get_account_balance,
    get_client,
    get_current_price,
)
from src.config import get_config
from src.controller import BotController
from src.database import get_session
from src.models import MarketData
from src.models import Position as PositionModel

INTERVAL = "1m"
POLL_SEC = 15
MAX_CANDLES = 100
PRICE_CRASH_CANDLES = 60
SNAP_INTERVAL_SEC = 6 * 3600


def _find_bb_columns(dataframe):
    """Find BB column names dynamically (compatible with different pandas-ta versions)."""
    lower_col = mid_col = None
    for col in dataframe.columns:
        if col.startswith("BBL_"):
            lower_col = col
        elif col.startswith("BBM_"):
            mid_col = col
    return lower_col, mid_col


class MarketPoller:
    """Fetches klines via REST, feeds candles to controller, triggers analysis."""

    def __init__(self, controller: BotController, profit_manager, on_position_closed):
        self.controller = controller
        self.profit_manager = profit_manager
        self._on_position_closed = on_position_closed
        self._last_status_log: dict[str, datetime] = {}
        self._last_snapshot = datetime.min.replace(tzinfo=timezone.utc)
        for symbol in controller.strategies:
            self._last_status_log[symbol] = datetime.min.replace(tzinfo=timezone.utc)

    def load_history(self):
        """Prime candle buffers with 50 historical candles."""
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

    def run(self):
        """Main polling loop — runs while controller.running is True."""
        client = get_client()
        while self.controller.running:
            for symbol in list(self.controller.candles.keys()):
                try:
                    klines = client.get_klines(
                        symbol=symbol, interval=INTERVAL, limit=3
                    )
                    for k in klines:
                        close_time = k[6]
                        if close_time <= self.controller.last_close_times.get(
                            symbol, 0
                        ):
                            continue
                        self.controller.last_close_times[symbol] = close_time
                        self._process_candle(symbol, k)
                except Exception as e:
                    logger.error(f"Poll {symbol}: {e}")
            time.sleep(POLL_SEC)

    def _process_candle(self, symbol: str, kline):
        """Process a single closed candle: update buffer, analyse, trade."""
        candle = {
            "open": float(kline[1]),
            "high": float(kline[2]),
            "low": float(kline[3]),
            "close": float(kline[4]),
            "volume": float(kline[5]),
        }
        buf = self.controller.candles[symbol]
        if buf and buf[-1].get("ot") == kline[0]:
            buf[-1] = candle
        else:
            buf.append(candle)

        if len(buf) < 25:
            return

        close_price = candle["close"]

        # --- Verbose status (per-symbol, every 60s) ---
        now = datetime.now(timezone.utc)
        if (
            now
            - self._last_status_log.get(
                symbol, datetime.min.replace(tzinfo=timezone.utc)
            )
        ).total_seconds() >= 60:
            self._last_status_log[symbol] = now
            self._log_market_status(symbol, buf, close_price)

        # --- Save candle to DB ---
        self._save_candle(symbol, kline, candle)

        # --- Breaker checks ---
        controller = self.controller
        if controller.risk_manager.day_start_balance > 0:
            controller.risk_manager.check_daily_drawdown(get_account_balance("USDT"))
        if len(buf) >= 15:
            df = pd.DataFrame(list(buf))
            atr = (df["high"] - df["low"]).tail(14).mean()
            if (atr / close_price) * 100 > 3.0:
                controller.risk_manager.check_atr_volatility((atr / close_price) * 100)
        if len(buf) >= PRICE_CRASH_CANDLES:
            price_1h_ago = list(buf)[-PRICE_CRASH_CANDLES]["close"]
            controller.risk_manager.check_price_crash_red(
                symbol, close_price, price_1h_ago
            )
            controller.risk_manager.check_price_decline_orange(
                symbol, close_price, price_1h_ago
            )

        # RED breaker = full stop
        if controller.risk_manager.breaker_level.value == "RED":
            logger.critical("RED BREAKER — stopping bot")
            self.controller.running = False
            return

        if not controller.risk_manager.is_trading_allowed()[0]:
            return

        # --- Position management ---
        session = get_session()
        position = (
            session.query(PositionModel)
            .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
            .first()
        )

        if position:
            # Check if OCO closed the position (asset balance = 0)
            asset = symbol.replace("USDT", "")
            balance = get_account_balance(asset)
            if (
                balance < float(position.quantity) * 0.5
                and float(position.quantity) > 0
            ):
                # OCO executed! Close in DB
                qty = float(position.quantity)
                pnl = (close_price - float(position.entry_price)) * qty
                position.current_price = close_price
                position.realized_pnl = pnl
                position.status = "CLOSED"
                position.closed_at = datetime.now(timezone.utc)
                session.commit()
                controller.risk_manager.record_trade(pnl)
                controller.trades_count += 1
                logger.info(f"OCO closed {symbol} @ {close_price:.2f} PnL={pnl:.2f}")
                session.close()
                if pnl > 0:
                    self.profit_manager.fix(pnl, symbol)
                self._on_position_closed(pnl)
            else:
                # Update unrealized PnL
                position.current_price = close_price
                position.unrealized_pnl = (
                    close_price - float(position.entry_price)
                ) * float(position.quantity)
                session.commit()
                session.close()
        else:
            session.close()
            # No position — check entry signal
            df = pd.DataFrame(list(buf))
            signal = self.controller.strategies[symbol].analyze(df)
            if signal.signal.value == "BUY":
                logger.info(f">> BUY {symbol} @ {signal.price:.2f} ({signal.reason})")
                if self.controller.executor.open_position(
                    symbol, signal.price, signal.stop_loss, signal.take_profit
                ):
                    controller.trades_count += 1

        controller.write_status()

        # Periodic snapshot
        if (
            datetime.now(timezone.utc) - self._last_snapshot
        ).total_seconds() > SNAP_INTERVAL_SEC:
            self._take_snapshot()

    def _log_market_status(self, symbol: str, buf: deque, close_price: float):
        """Log BB + RSI indicators for the symbol."""
        try:
            import pandas_ta as ta

            df = pd.DataFrame(list(buf))
            close_series = df["close"]
            bb = ta.bbands(close_series, length=20, std=2)
            rsi_series = ta.rsi(close_series, length=14)

            lower_col, mid_col = (
                _find_bb_columns(bb) if bb is not None else (None, None)
            )
            lower = float(bb.iloc[-1][lower_col]) if lower_col else 0
            mid = float(bb.iloc[-1][mid_col]) if mid_col else 0
            rsi_val = (
                float(rsi_series.iloc[-1])
                if rsi_series is not None and not pd.isna(rsi_series.iloc[-1])
                else 0
            )
            sig = "BUY" if lower and close_price <= lower and rsi_val < 40 else "HOLD"

            # Get position info
            pos_str = ""
            session = get_session()
            pos = (
                session.query(PositionModel)
                .filter(PositionModel.symbol == symbol, PositionModel.status == "OPEN")
                .first()
            )
            if pos:
                pos_str = f" | POS: ent={float(pos.entry_price):.2f} SL={float(pos.stop_loss or 0):.2f} TP={float(pos.take_profit or 0):.2f}"
            session.close()

            logger.info(
                f"[{symbol}] {close_price:.2f} | BB:{lower:.2f}/{mid:.2f} | RSI:{rsi_val:.1f} | {sig}{pos_str}"
            )
        except Exception as e:
            logger.info(f"[{symbol}] {close_price:.2f} | indicators error: {e}")

    def _save_candle(self, symbol: str, kline, candle: dict):
        """Save closed candle to market_data table."""
        try:
            session = get_session()
            open_time = datetime.fromtimestamp(kline[0] / 1000, tz=timezone.utc)
            if (
                not session.query(MarketData)
                .filter(
                    MarketData.symbol == symbol,
                    MarketData.interval == INTERVAL,
                    MarketData.open_time == open_time,
                )
                .first()
            ):
                session.add(
                    MarketData(
                        symbol=symbol,
                        interval=INTERVAL,
                        open_time=open_time,
                        close_time=datetime.fromtimestamp(
                            kline[6] / 1000, tz=timezone.utc
                        ),
                        open=candle["open"],
                        high=candle["high"],
                        low=candle["low"],
                        close=candle["close"],
                        volume=candle["volume"],
                        trades_count=kline[8],
                    )
                )
                session.commit()
            session.close()
        except Exception:
            pass  # Non-critical

    def _take_snapshot(self):
        """Save account balance snapshot to DB."""
        try:
            balance = get_account_balance("USDT")
            session = get_session()
            from src.models import AccountSnapshot

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
