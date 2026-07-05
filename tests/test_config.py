"""Unit tests for config module."""
from __future__ import annotations
import pytest
import src.config as cfg
from src.config import AppConfig, BotConfig, PostgresConfig, BinanceConfig, get_config

class TestPostgresConfig:
    def test_defaults(self): c = PostgresConfig(); assert c.host == "localhost"; assert c.port == 5432
    def test_url_encoding(self): c = PostgresConfig(password="p@ss:word!"); assert "p%40ss%3Aword%21" in c.url
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_HOST","db.example.com"); monkeypatch.setenv("POSTGRES_PORT","5433"); monkeypatch.setenv("POSTGRES_DB","test_db")
        cfg._config = None; c = get_config().postgres; assert c.host == "db.example.com"; assert c.port == 5433

class TestBinanceConfig:
    def test_defaults(self): c = BinanceConfig(); assert c.api_key == ""; assert c.testnet is True
    def test_testnet_false(self, monkeypatch): monkeypatch.setenv("BINANCE_TESTNET","false"); cfg._config = None; assert get_config().binance.testnet is False

class TestBotConfig:
    def test_defaults(self): c = BotConfig(); assert "BTCUSDT" in c.symbols; assert c.trade_amount_pct == 10.0; assert c.max_trades_per_day == 30
    def test_override_from_db(self): c = BotConfig(); c.override_from_db({"risk_per_trade":"5","max_trades_per_day":"50"}); assert c.risk_per_trade == 5.0; assert c.max_trades_per_day == 50
    def test_override_invalid(self): c = BotConfig(); c.override_from_db({"risk_per_trade":"abc"}); assert c.risk_per_trade == 1.0
    def test_override_rsi(self): c = BotConfig(); c.override_from_db({"use_strict_rsi":"true"}); assert c.use_strict_rsi is True; c.override_from_db({"use_strict_rsi":"0"}); assert c.use_strict_rsi is False

class TestAppConfig:
    def test_singleton(self): cfg._config = None; c1 = get_config(); c2 = get_config(); assert c1 is c2
