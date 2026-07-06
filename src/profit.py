"""Profit fixation: USDT -> buy BTC/keep USDT -> transfer to Funding wallet."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from src.binance_client import place_market_buy, transfer_to_funding
from src.database import get_session
from src.models import BotLog

PROFIT_FILE = Path(__file__).resolve().parents[1] / "profit_state.json"


class ProfitManager:
    """Accumulates profit: BTCUSDT -> BTC, ETHUSDT -> USDT. Sends to Funding wallet."""

    def __init__(self):
        self.total_btc = 0.0
        self.total_usdt = 0.0
        self._load()

    def _load(self):
        if PROFIT_FILE.exists():
            try:
                data = json.loads(PROFIT_FILE.read_text())
                self.total_btc = data.get("total_btc", 0.0)
                self.total_usdt = data.get("total_usdt", 0.0)
            except Exception:
                pass

    def _save(self):
        PROFIT_FILE.write_text(
            json.dumps(
                {
                    "total_btc": round(self.total_btc, 8),
                    "total_usdt": round(self.total_usdt, 4),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    def fix(self, profit_usdt: float, symbol: str):
        """Convert profit and transfer to Funding wallet."""
        if profit_usdt <= 0:
            return

        if "BTC" in symbol:
            self._fix_to_btc(profit_usdt)
        else:
            self._fix_to_usdt(profit_usdt)

    def _fix_to_btc(self, profit_usdt: float):
        if profit_usdt < 15:
            logger.info(f"BTC profit {profit_usdt:.2f} USDT — accumulating")
            return
        logger.info(f">>> Buying BTC for {profit_usdt:.2f} USDT")
        order = place_market_buy("BTCUSDT", profit_usdt)
        if not order or not order.get("fills"):
            logger.error("Buy BTC failed")
            return
        btc_bought = sum(float(f["qty"]) for f in order["fills"])
        transfer_to_funding("BTC", btc_bought)
        self.total_btc += btc_bought
        self._save()
        logger.info(
            f"PROFIT: {profit_usdt:.2f} USDT -> {btc_bought:.8f} BTC | Total: {self.total_btc:.8f}"
        )
        self._log(
            f"+{profit_usdt:.2f} USDT -> {btc_bought:.8f} BTC | Total BTC: {self.total_btc:.8f}"
        )

    def _fix_to_usdt(self, profit_usdt: float):
        transfer_to_funding("USDT", profit_usdt)
        self.total_usdt += profit_usdt
        self._save()
        logger.info(
            f"PROFIT: {profit_usdt:.2f} USDT -> Funding | Total: {self.total_usdt:.2f}"
        )
        self._log(
            f"+{profit_usdt:.2f} USDT -> Funding | Total USDT: {self.total_usdt:.2f}"
        )

    def _log(self, message: str):
        try:
            session = get_session()
            session.add(BotLog(level="INFO", category="profit", message=message))
            session.commit()
            session.close()
        except Exception as e:
            logger.error(f"Profit log error: {e}")
