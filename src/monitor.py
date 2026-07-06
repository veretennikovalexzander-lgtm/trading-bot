"""Network monitor: ping health checks."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from loguru import logger
from src.binance_client import ping

PING_INTERVAL_SEC = 30
MAX_PING_FAILURES = 3
MAX_WS_RECONNECTS = 5
WS_RECONNECT_WINDOW_MIN = 10


class NetworkWatchdog:
    """Background thread: checks Binance connectivity every 30s."""

    def __init__(self, on_critical):
        self._on_critical = on_critical
        self._stop_event = threading.Event()
        self.ping_failures = 0
        self.ws_disconnects: list[datetime] = []

    def start(self):
        self._stop_event.clear()
        threading.Thread(target=self._run, daemon=True).start()
        logger.info("Network watchdog started")

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(PING_INTERVAL_SEC)
            if self._stop_event.is_set():
                break
            if ping():
                self.ping_failures = 0
            else:
                self.ping_failures += 1
                logger.warning(f"Ping fail {self.ping_failures}/{MAX_PING_FAILURES}")
            if self.ping_failures >= MAX_PING_FAILURES:
                logger.critical("NETWORK LOST — stopping bot")
                self._on_critical()
                return
            # Prune WS disconnect list
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=WS_RECONNECT_WINDOW_MIN
            )
            self.ws_disconnects = [t for t in self.ws_disconnects if t > cutoff]

    def record_ws_disconnect(self):
        self.ws_disconnects.append(datetime.now(timezone.utc))
        if len(self.ws_disconnects) >= MAX_WS_RECONNECTS:
            logger.critical("NETWORK UNSTABLE — stopping bot")
            self._on_critical()
