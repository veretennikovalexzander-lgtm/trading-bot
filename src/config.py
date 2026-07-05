"""
Configuration loader: .env + bot_config from database (FR-4.7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, "").lower() in ("true", "1", "yes")


@dataclass
class PostgresConfig:
    host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5432))
    db: str = field(default_factory=lambda: _env("POSTGRES_DB", "trading_bot"))
    user: str = field(default_factory=lambda: _env("POSTGRES_USER", "bot_user"))
    password: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "changeme"))

    @property
    def url(self) -> str:
        return (
            f"postgresql://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.db}"
        )


@dataclass
class BinanceConfig:
    api_key: str = field(default_factory=lambda: _env("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _env("BINANCE_API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: _env_bool("BINANCE_TESTNET", True))


@dataclass
class BotConfig:
    strategy: str = field(default_factory=lambda: _env("BOT_STRATEGY", "bollinger_rsi"))
    symbols: list[str] = field(
        default_factory=lambda: [
            s.strip()
            for s in _env("BOT_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
            if s.strip()
        ]
    )
    trade_amount_pct: float = field(
        default_factory=lambda: _env_float("BOT_TRADE_AMOUNT_PCT", 10.0)
    )
    max_positions: int = field(default_factory=lambda: _env_int("BOT_MAX_POSITIONS", 5))
    risk_per_trade: float = field(
        default_factory=lambda: _env_float("BOT_RISK_PER_TRADE", 1.0)
    )
    max_trades_per_day: int = field(
        default_factory=lambda: _env_int("BOT_MAX_TRADES_PER_DAY", 30)
    )
    profit_target: float = field(
        default_factory=lambda: _env_float("BOT_PROFIT_TARGET", 100.0)
    )
    use_strict_rsi: bool = field(
        default_factory=lambda: _env_bool("BOT_STRICT_RSI", False)
    )

    def override_from_db(self, db_configs: dict[str, str]):
        """FR-4.7: Override settings from bot_config table at runtime."""
        for key, value in db_configs.items():
            try:
                if key == "risk_per_trade":
                    self.risk_per_trade = float(value)
                elif key == "max_trades_per_day":
                    self.max_trades_per_day = int(value)
                elif key == "trade_amount_pct":
                    self.trade_amount_pct = float(value)
                elif key == "use_strict_rsi":
                    self.use_strict_rsi = value.lower() in ("true", "1")
                elif key == "strategy":
                    self.strategy = value
                elif key == "max_positions":
                    self.max_positions = int(value)
            except (ValueError, TypeError):
                from loguru import logger

                logger.warning(f"Invalid config value for {key}: {value}")


@dataclass
class AppConfig:
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    bot: BotConfig = field(default_factory=BotConfig)


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def reload_from_db(session=None):
    """FR-4.7: Reload bot config from database."""
    if session is None:
        try:
            from src.database import get_session

            session = get_session()
            should_close = True
        except Exception:
            from loguru import logger

            logger.warning("Cannot create session for config reload")
            return
    else:
        should_close = False

    try:
        from src.models import BotConfig as BotConfigModel

        rows = session.query(BotConfigModel).all()
        db_configs = {r.config_key: r.config_value for r in rows}
        get_config().bot.override_from_db(db_configs)
        from loguru import logger

        logger.debug(f"Config reloaded from DB: {list(db_configs.keys())}")
    except Exception as e:
        from loguru import logger

        logger.warning(f"Could not reload config from DB: {e}")
    finally:
        if should_close:
            session.close()
