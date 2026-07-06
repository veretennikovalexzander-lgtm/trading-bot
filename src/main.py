"""Trading Bot — entry point. Usage: python -m src.main [start|stop|status|close|logs]"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone

from loguru import logger
from src.binance_client import get_account_balance, ping
from src.cli import close_position, show_logs, show_status
from src.config import get_config
from src.controller import BotController
from src.database import init_db
from src.logger import setup_logging
from src.monitor import NetworkWatchdog
from src.poller import MarketPoller
from src.profit import ProfitManager


def run_bot():
    """Start the trading bot: controller + watchdog + poller."""
    setup_logging()
    init_db()

    if not ping():
        logger.error("Binance unreachable")
        sys.exit(1)
    logger.info("Network OK")

    controller = BotController()
    profit_manager = ProfitManager()
    controller.risk_manager.day_start_balance = get_account_balance("USDT")

    # Network watchdog
    watchdog = NetworkWatchdog(on_critical=controller.stop)

    # Market poller
    poller = MarketPoller(
        controller, profit_manager, on_position_closed=lambda pnl: None
    )

    # Start
    controller.running = True
    controller.started_at = datetime.now(timezone.utc)
    watchdog.start()
    poller.load_history()
    controller.write_status(
        {
            "profit_btc": profit_manager.total_btc,
            "profit_usdt": profit_manager.total_usdt,
        }
    )
    logger.info("=== Bot STARTED ===")

    try:
        poller.run()
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


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) >= 2 else ""
    if cmd == "status":
        show_status()
    elif cmd == "close" and len(sys.argv) >= 3:
        close_position(sys.argv[2].upper())
    elif cmd == "logs":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 20
        show_logs(n)
    elif cmd == "start":
        run_bot()
    elif cmd == "stop":
        print("Stop is handled by Ctrl+C in the running process")
    else:
        print("Usage: python -m src.main [start|stop|status|close SYMBOL|logs N]")


if __name__ == "__main__":
    main()
