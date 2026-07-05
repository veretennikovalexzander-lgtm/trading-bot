"""
Configuration loader: .env + bot_config from database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").lower()
    return val in ("true", "1", "yes")


@dataclass
class PostgresConfig:
    host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5432))
    db: str = field(default_factory=lambda: _env("POSTGRES_DB", "trading_bot"))
    user: str = field(default_factory=lambda: _env("POSTGRES_USER", "bot_user"))
    password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "changeme"))

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


@dataclass
class BinanceConfig:
    api_key: str = field(default_factory=lambda: _env("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _env("BINANCE_API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: _env_bool("BINANCE_TESTNET", True))


@dataclass
class BotConfig:
    strategy: str = field(default_factory=lambda: _env("BOT_STRATEGY", "bollinger_rsi"))
    symbols: list[str] = field(
        default_factory=lambda: [s.strip() for s in _env("BOT_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    )
    trade_amount_pct: float = field(default_factory=lambda: _env_float("BOT_TRADE_AMOUNT_PCT", 10.0))  # % of balance
    max_positions: int = field(default_factory=lambda: _env_int("BOT_MAX_POSITIONS", 5))
    risk_per_trade: float = field(default_factory=lambda: _env_float("BOT_RISK_PER_TRADE", 1.0))  # %
    max_trades_per_day: int = field(default_factory=lambda: _env_int("BOT_MAX_TRADES_PER_DAY", 30))
    profit_target: float = field(default_factory=lambda: _env_float("BOT_PROFIT_TARGET", 100.0))  # USDT base
    telegram_enabled: bool = field(default_factory=lambda: _env_bool("TELEGRAM_ENABLED", False))
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_TOKEN", ""))
    telegram_chat_ids: list[int] = field(
        default_factory=lambda: [
            int(x.strip()) for x in _env("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()
        ]
    )


@dataclass
class AppConfig:
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    bot: BotConfig = field(default_factory=BotConfig)


# Singleton
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config
