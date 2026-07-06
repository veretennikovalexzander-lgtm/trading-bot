"""Trading Bot — async entry point. Usage: python -m src.main [start|status|monitor|close|logs]"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from loguru import logger
from src.binance_client import get_account_balance, ping
from src.cli import close_position, show_logs, show_monitor, show_status
from src.config import get_config
from src.controller import BotController
from src.database import init_db
from src.logger import setup_logging
from src.monitor import NetworkWatchdog
from src.profit import ProfitManager
from src.streamer import WebSocketStreamer


def _validate_config():
    cfg = get_config()
    dangerous = ("your_api_key_here", "your_api_secret_here")
    if cfg.binance.api_key.lower() in dangerous or not cfg.binance.api_key:
        logger.error("SECURITY: API key not set")
        sys.exit(1)
    if cfg.binance.api_secret.lower() in dangerous or not cfg.binance.api_secret:
        logger.error("SECURITY: API secret not set")
        sys.exit(1)


async def _snapshot_loop(streamer: WebSocketStreamer):
    while streamer.controller.running:
        await asyncio.sleep(6 * 3600)
        if streamer.controller.running:
            streamer._take_snapshot()


async def run_bot_async():
    setup_logging()
    _validate_config()
    init_db()

    if not ping():
        logger.error("Binance unreachable")
        sys.exit(1)
    logger.info("Network OK")

    controller = BotController()
    profit_manager = ProfitManager()
    controller.risk_manager.day_start_balance = get_account_balance("USDT")
    watchdog = NetworkWatchdog(on_critical=controller.stop)
    streamer = WebSocketStreamer(
        controller, profit_manager, on_position_closed=lambda pnl: None
    )

    controller.running = True
    controller.started_at = datetime.now(timezone.utc)
    watchdog.start()
    streamer.load_history()
    controller.write_status(
        {
            "profit_btc": profit_manager.total_btc,
            "profit_usdt": profit_manager.total_usdt,
        }
    )
    logger.info("=== Bot STARTED (async WebSocket) ===")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(streamer.run())
            tg.create_task(_snapshot_loop(streamer))
    except KeyboardInterrupt:
        pass
    finally:
        controller.running = False
        watchdog.stop()
        controller.write_status(
            {
                "profit_btc": profit_manager.total_btc,
                "profit_usdt": profit_manager.total_usdt,
            }
        )
        logger.info("=== Bot STOPPED ===")


def run_bot():
    asyncio.run(run_bot_async())


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""
    if cmd == "status":
        show_status()
    elif cmd == "monitor":
        sec = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        show_monitor(sec)
    elif cmd == "close" and len(sys.argv) >= 3:
        close_position(sys.argv[2].upper())
    elif cmd == "logs":
        show_logs(int(sys.argv[2]) if len(sys.argv) >= 3 else 20)
    elif cmd == "start":
        run_bot()
    elif cmd == "stop":
        print("Stop is handled by Ctrl+C")
    else:
        print("Commands: start | stop | status | monitor | close SYMBOL | logs N")


if __name__ == "__main__":
    main()
