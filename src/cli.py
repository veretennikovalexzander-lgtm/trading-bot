"""CLI commands: status, close, logs (independent of running controller)."""

from __future__ import annotations

import json
from pathlib import Path

from src.binance_client import get_account_balance, get_current_price, ping
from src.controller import STATUS_FILE
from src.database import get_session
from src.models import BotLog
from src.models import Position as PositionModel
from src.risk_manager import RiskManager

INTERVAL = "1m"


def show_status():
    risk = RiskManager()
    daily = risk.get_daily_stats()

    session = get_session()
    positions = (
        session.query(PositionModel).filter(PositionModel.status == "OPEN").all()
    )
    session.close()

    running = False
    profit_btc = 0.0
    profit_usdt = 0.0
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
            running = data.get("running", False)
            profit_btc = data.get("profit_btc", 0.0)
            profit_usdt = data.get("profit_usdt", 0.0)
        except Exception:
            pass

    balance = get_account_balance("USDT")
    net = "OK" if ping() else "FAIL"

    print(f"\n{'=' * 50}")
    print(f"  Trading Bot ({INTERVAL})")
    print(f"{'=' * 50}")
    print(f"  Running: {running} | Network: {net}")
    print(f"  Balance: {balance:.2f} USDT")
    print(f"  Profit: {profit_btc:.8f} BTC | {profit_usdt:.2f} USDT")
    print(f"  Trades: {daily['trades']} | PnL: {daily['pnl']} USDT")
    print(f"  Breaker: {daily['breaker']}")
    if positions:
        print(f"\n  Positions ({len(positions)}):")
        for p in positions:
            print(
                f"    {p.symbol} ent={float(p.entry_price):.2f} qty={float(p.quantity):.6f} SL={float(p.stop_loss or 0):.2f}"
            )
    else:
        print("\n  Positions: 0")
    print(f"{'=' * 50}\n")


def close_position(symbol: str):
    session = get_session()
    pos = (
        session.query(PositionModel)
        .filter(PositionModel.symbol == symbol.upper(), PositionModel.status == "OPEN")
        .first()
    )
    session.close()
    if not pos:
        print(f"No open position: {symbol}")
        return
    from src.trade_executor import TradeExecutor

    executor = TradeExecutor()
    price = get_current_price(symbol.upper())
    ok = executor.close_position(pos, price, reason="manual")
    print(f"{'Closed' if ok else 'Failed to close'} {symbol}")


def show_logs(n: int = 20):
    session = get_session()
    logs = session.query(BotLog).order_by(BotLog.created_at.desc()).limit(n).all()
    session.close()
    for entry in reversed(logs):
        print(f"[{entry.level}] [{entry.category or '-'}] {entry.message}")
