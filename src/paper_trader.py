"""
Paper Trading: simulate trades using real Binance klines (FR-6.2).

Usage: python -m src.paper_trader [start|status]
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger
from src.binance_client import get_client, get_current_price
from src.config import get_config
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy

PAPER_FILE = Path(__file__).resolve().parents[1] / "paper_account.json"
MAX_CANDLES = 100
INTERVAL = "1m"
CHECK_EVERY_SEC = 15


class PaperTrader:
    def __init__(self):
        self.balance = 100.0
        self.positions: dict[str, dict] = {}
        self.trade_log: list[dict] = []
        self._candles: dict[str, deque[dict]] = {}
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.risk_manager = RiskManager()
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(
                symbol=symbol,
                interval=INTERVAL,
                use_strict_filter=cfg.bot.use_strict_rsi,
            )
            self._candles[symbol] = deque(maxlen=MAX_CANDLES)
        self._load_state()
        self._load_historical()

    def _save_state(self):
        PAPER_FILE.write_text(
            json.dumps(
                {
                    "balance": round(self.balance, 2),
                    "positions": self.positions,
                    "trade_log": self.trade_log[-100:],
                },
                indent=2,
            )
        )

    def _load_state(self):
        if PAPER_FILE.exists():
            try:
                d = json.loads(PAPER_FILE.read_text())
                self.balance = d.get("balance", 100.0)
                self.positions = d.get("positions", {})
                self.trade_log = d.get("trade_log", [])
                logger.info(f"Paper account: {self.balance:.2f} USDT")
            except Exception:
                pass

    def _load_historical(self):
        """Load real klines from Binance REST."""
        client = get_client()
        for symbol in self._candles:
            try:
                klines = client.get_klines(symbol=symbol, interval=INTERVAL, limit=50)
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
                logger.info(f"Paper: loaded {len(klines)} candles for {symbol}")
            except Exception as e:
                logger.error(f"Paper: failed to load {symbol}: {e}")

    def show_status(self):
        pnl_total = sum(t.get("pnl", 0) for t in self.trade_log)
        print(f"\n{'=' * 45}")
        print("  Paper Trading Status")
        print(f"{'=' * 45}")
        print(f"  Balance: {self.balance:.2f} USDT")
        print(f"  Total PnL: {pnl_total:.2f} USDT | Trades: {len(self.trade_log)}")
        if self.positions:
            print(f"\n  Positions ({len(self.positions)}):")
            for sym, pos in self.positions.items():
                cur = get_current_price(sym)
                pnl = (cur - pos["entry"]) * pos["qty"]
                print(
                    f"    {sym} entry={pos['entry']:.2f} qty={pos['qty']:.6f} PnL={pnl:.2f}"
                )
        else:
            print("\n  Positions: 0")
        if self.trade_log:
            print(f"\n  Last 5 trades:")
            for t in self.trade_log[-5:]:
                print(
                    f"    {t['symbol']} {t['side']} @ {t['price']:.2f} PnL={t['pnl']:.2f}"
                )
        buf_sizes = " | ".join(f"{s}={len(self._candles[s])}" for s in self._candles)
        print(f"\n  Buffers: {buf_sizes}")
        print(f"{'=' * 45}\n")

    def run(self):
        logger.info("=== Paper Trading STARTED ===")
        while True:
            # Update latest candle with current price
            for symbol in self._candles:
                price = get_current_price(symbol)
                if price <= 0:
                    continue
                buf = self._candles[symbol]
                if buf:
                    last = buf[-1]
                    last["high"] = max(last["high"], price)
                    last["low"] = min(last["low"], price)
                    last["close"] = price

            # Check signals
            for symbol, buf in self._candles.items():
                if len(buf) < 25:
                    continue
                if not self.risk_manager.is_trading_allowed()[0]:
                    continue

                price = get_current_price(symbol)
                if price <= 0:
                    continue

                # Position TP/SL
                if symbol in self.positions:
                    pos = self.positions[symbol]
                    if price >= pos["tp"]:
                        pnl = (price - pos["entry"]) * pos["qty"]
                        self.balance += pos["qty"] * price
                        self.trade_log.append(
                            {
                                "symbol": symbol,
                                "side": "SELL",
                                "price": price,
                                "pnl": pnl,
                            }
                        )
                        logger.info(
                            f"Paper TP {symbol}: +{pnl:.2f} USDT (balance: {self.balance:.2f})"
                        )
                        del self.positions[symbol]
                        self.risk_manager.record_trade(pnl)
                        self._save_state()
                    elif price <= pos["sl"]:
                        pnl = (price - pos["entry"]) * pos["qty"]
                        self.balance += pos["qty"] * price
                        self.trade_log.append(
                            {
                                "symbol": symbol,
                                "side": "SELL",
                                "price": price,
                                "pnl": pnl,
                            }
                        )
                        logger.info(
                            f"Paper SL {symbol}: {pnl:.2f} USDT (balance: {self.balance:.2f})"
                        )
                        del self.positions[symbol]
                        self.risk_manager.record_trade(pnl)
                        self._save_state()
                    continue

                # Entry signal
                df = pd.DataFrame(list(buf))
                signal = self.strategies[symbol].analyze(df)
                if signal.signal.value == "BUY":
                    cfg = get_config()
                    trade_amount = self.balance * (cfg.bot.trade_amount_pct / 100)
                    qty = trade_amount / signal.price
                    cost = qty * signal.price
                    if cost <= self.balance:
                        self.balance -= cost
                        self.positions[symbol] = {
                            "entry": signal.price,
                            "qty": qty,
                            "sl": signal.stop_loss,
                            "tp": signal.take_profit,
                        }
                        self.trade_log.append(
                            {
                                "symbol": symbol,
                                "side": "BUY",
                                "price": signal.price,
                                "pnl": 0,
                            }
                        )
                        logger.info(
                            f"Paper BUY {symbol} @ {signal.price:.2f} qty={qty:.6f} ({signal.reason})"
                        )
                        self._save_state()

            time.sleep(CHECK_EVERY_SEC)


def main():
    from src.logger import setup_logging

    setup_logging()
    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""
    trader = PaperTrader()
    if cmd == "start":
        trader.run()
    elif cmd == "status":
        trader.show_status()
    else:
        print("Commands: start | status")


if __name__ == "__main__":
    main()
