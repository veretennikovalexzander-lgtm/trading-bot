"""
Paper Trading: simulate trades without real orders (FR-6.2).

Usage:
    python -m src.paper_trader start    # Run paper trading
    python -m src.paper_trader status   # Show paper account status
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from src.binance_client import get_client, get_current_price
from src.config import get_config
from src.risk_manager import RiskManager
from src.strategy.bollinger_rsi import BollingerRSIStrategy

PAPER_FILE = Path(__file__).resolve().parents[1] / "paper_account.json"
MAX_CANDLES = 100


class PaperTrader:
    def __init__(self):
        self.balance = 100.0  # Start with 100 USDT
        self.positions: dict[str, dict] = {}  # symbol → {entry, qty, sl, tp}
        self.trade_log: list[dict] = []
        self._candles: dict[str, deque[dict]] = {}
        self.strategies: dict[str, BollingerRSIStrategy] = {}
        self.risk_manager = RiskManager()
        cfg = get_config()
        for symbol in cfg.bot.symbols:
            self.strategies[symbol] = BollingerRSIStrategy(
                symbol=symbol, use_strict_filter=cfg.bot.use_strict_rsi
            )
            self._candles[symbol] = deque(maxlen=MAX_CANDLES)
        self._load_state()

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
                data = json.loads(PAPER_FILE.read_text())
                self.balance = data.get("balance", 100.0)
                self.positions = data.get("positions", {})
                self.trade_log = data.get("trade_log", [])
                logger.info(f"Paper account loaded: {self.balance:.2f} USDT")
            except Exception:
                pass

    def show_status(self):
        print(f"\n{'=' * 40}")
        print(" Paper Trading Status")
        print(f"{'=' * 40}")
        print(f" Balance: {self.balance:.2f} USDT")
        total_pnl = sum(t.get("pnl", 0) for t in self.trade_log)
        print(f" Total PnL: {total_pnl:.2f} USDT")
        print(f" Total trades: {len(self.trade_log)}")
        if self.positions:
            print(f"\n Positions ({len(self.positions)}):")
            for sym, pos in self.positions.items():
                price = get_current_price(sym)
                pnl = (price - pos["entry"]) * pos["qty"]
                print(
                    f"  {sym} entry={pos['entry']:.2f} qty={pos['qty']:.6f} PnL={pnl:.2f}"
                )
        else:
            print("\n Positions: 0")
        if self.trade_log:
            print(f"\n Last 5 trades:")
            for t in self.trade_log[-5:]:
                print(
                    f"  {t['symbol']} {t['side']} @ {t['price']:.2f} PnL={t['pnl']:.2f}"
                )
        print(f"{'=' * 40}\n")

    def run(self):
        logger.info("=== Paper Trading STARTED ===")
        client = get_client()

        # Load historical candles
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
                        }
                    )
                logger.info(f"Paper: loaded {len(klines)} candles for {symbol}")
            except Exception as e:
                logger.error(f"Paper: failed to load candles for {symbol}: {e}")

        import pandas as pd

        while True:
            for symbol, buf in self._candles.items():
                # Get latest price and add to buffer
                price = get_current_price(symbol)
                if price <= 0:
                    continue

                last_candle = buf[-1] if buf else None
                if last_candle:
                    last_candle["close"] = price
                    last_candle["high"] = max(last_candle["high"], price)
                    last_candle["low"] = min(last_candle["low"], price)
                else:
                    buf.append(
                        {
                            "open": price,
                            "high": price,
                            "low": price,
                            "close": price,
                            "volume": 0,
                        }
                    )

                if len(buf) < 25:
                    continue

                allowed, _ = self.risk_manager.is_trading_allowed()
                if not allowed:
                    continue

                # Check position TP/SL
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
                        logger.info(f"Paper TP {symbol}: +{pnl:.2f} USDT")
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
                        logger.info(f"Paper SL {symbol}: {pnl:.2f} USDT")
                        del self.positions[symbol]
                        self.risk_manager.record_trade(pnl)
                        self._save_state()
                    continue

                # Check entry signal
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

            time.sleep(10)  # Check every 10 seconds


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
