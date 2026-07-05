"""
Binance API client wrapper: REST orders + WebSocket market data.
"""
from __future__ import annotations

from typing import Any, Callable

from binance.client import Client
from binance.enums import ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET, SIDE_BUY, SIDE_SELL, TIME_IN_FORCE_GTC
from binance.exceptions import BinanceAPIException, BinanceOrderException
from loguru import logger

from src.config import get_config

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        cfg = get_config()
        _client = Client(
            api_key=cfg.binance.api_key,
            api_secret=cfg.binance.api_secret,
            testnet=cfg.binance.testnet,
        )
        logger.info(f"Binance client initialized (testnet={cfg.binance.testnet})")
    return _client


def ping() -> bool:
    try:
        get_client().ping()
        logger.debug("Binance ping OK")
        return True
    except Exception as e:
        logger.error(f"Binance ping failed: {e}")
        return False


def get_account_balance(asset: str = "USDT") -> float:
    try:
        balances = get_client().get_account()["balances"]
        for b in balances:
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        logger.error(f"Failed to get balance: {e}")
    return 0.0


def get_symbol_info(symbol: str) -> dict[str, Any]:
    """Get LOT_SIZE, tick_size, step_size for a symbol."""
    try:
        info = get_client().get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                return {
                    "min_qty": float(f["minQty"]),
                    "step_size": float(f["stepSize"]),
                }
    except Exception as e:
        logger.error(f"Failed to get symbol info for {symbol}: {e}")
    return {"min_qty": 0.0, "step_size": 0.0}


def get_current_price(symbol: str) -> float:
    try:
        ticker = get_client().get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        logger.error(f"Failed to get price for {symbol}: {e}")
    return 0.0


def place_limit_buy(symbol: str, quantity: float, price: float) -> dict | None:
    """Place a limit buy order."""
    try:
        order = get_client().order_limit_buy(
            symbol=symbol,
            quantity=quantity,
            price=str(round(price, 2)),
        )
        logger.info(f"BUY order placed: {symbol} qty={quantity} @ {price}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"Failed to place BUY for {symbol}: {e}")
        return None


def place_limit_sell(symbol: str, quantity: float, price: float) -> dict | None:
    """Place a limit sell order."""
    try:
        order = get_client().order_limit_sell(
            symbol=symbol,
            quantity=quantity,
            price=str(round(price, 2)),
        )
        logger.info(f"SELL order placed: {symbol} qty={quantity} @ {price}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"Failed to place SELL for {symbol}: {e}")
        return None


def place_market_sell(symbol: str, quantity: float) -> dict | None:
    """Emergency market sell (for circuit breaker)."""
    try:
        order = get_client().order_market_sell(symbol=symbol, quantity=quantity)
        logger.warning(f"MARKET SELL placed: {symbol} qty={quantity}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"Failed market sell for {symbol}: {e}")
        return None


def get_order_status(symbol: str, order_id: str) -> dict | None:
    try:
        return get_client().get_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {e}")
    return None


def start_websocket(symbols: list[str], callback: Callable[[dict], None]):
    """Start Binance WebSocket for kline data."""
    from binance import ThreadedWebsocketManager

    twm = ThreadedWebsocketManager(
        api_key=get_config().binance.api_key,
        api_secret=get_config().binance.api_secret,
        testnet=get_config().binance.testnet,
    )
    twm.start()

    streams = [f"{s.lower()}@kline_5m" for s in symbols]
    twm.start_multiplex_socket(callback=callback, streams=streams)

    logger.info(f"WebSocket started for: {symbols}")
    return twm
