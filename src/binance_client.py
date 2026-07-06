"""
Binance API client wrapper: REST orders + WebSocket market data.
"""

from __future__ import annotations

from typing import Any

from binance.client import Client
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
        key_masked = cfg.binance.api_key[:4] + "****" + cfg.binance.api_key[-4:] if len(cfg.binance.api_key) > 8 else "****"
        logger.info(f"Binance client initialized (testnet={cfg.binance.testnet}, key={key_masked})")
    return _client


def ping() -> bool:
    try:
        get_client().ping()
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
    """Get LOT_SIZE, step_size, tick_size, min_notional for a symbol."""
    try:
        info = get_client().get_symbol_info(symbol)
        result = {"min_qty": 0.0, "step_size": 0.0, "tick_size": 0.01}
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                result["min_qty"] = float(f["minQty"])
                result["step_size"] = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                result["tick_size"] = float(f["tickSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                result["min_notional"] = float(f.get("minNotional", 0))
        return result
    except Exception as e:
        logger.error(f"Failed to get symbol info for {symbol}: {e}")
    return {"min_qty": 0.0, "step_size": 0.0, "tick_size": 0.01}


def get_current_price(symbol: str) -> float:
    try:
        ticker = get_client().get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        logger.error(f"Failed to get price for {symbol}: {e}")
    return 0.0


def _round_price(price: float, tick_size: float) -> str:
    """Round price to tick_size precision."""
    if tick_size <= 0:
        return f"{price:.8f}"
    precision = 0
    ts = tick_size
    while ts < 1:
        ts *= 10
        precision += 1
    return f"{round(price / tick_size) * tick_size:.{precision}f}"


def place_limit_buy(symbol: str, quantity: float, price: float) -> dict | None:
    try:
        info = get_symbol_info(symbol)
        order = get_client().order_limit_buy(
            symbol=symbol,
            quantity=quantity,
            price=_round_price(price, info["tick_size"]),
        )
        logger.info(f"BUY: {symbol} qty={quantity} @ {price}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"BUY failed {symbol}: {e}")
        return None


def place_limit_sell(symbol: str, quantity: float, price: float) -> dict | None:
    try:
        info = get_symbol_info(symbol)
        order = get_client().order_limit_sell(
            symbol=symbol,
            quantity=quantity,
            price=_round_price(price, info["tick_size"]),
        )
        logger.info(f"SELL: {symbol} qty={quantity} @ {price}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"SELL failed {symbol}: {e}")
        return None


def place_market_buy(symbol: str, quote_order_qty: float) -> dict | None:
    """Market buy using quoteOrderQty (spend exact USDT amount)."""
    try:
        order = get_client().order_market_buy(
            symbol=symbol,
            quoteOrderQty=quote_order_qty,
        )
        logger.info(f"MARKET BUY: {symbol} amount={quote_order_qty} USDT")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"MARKET BUY failed {symbol}: {e}")
        return None


def place_market_sell(symbol: str, quantity: float) -> dict | None:
    try:
        order = get_client().order_market_sell(symbol=symbol, quantity=quantity)
        logger.warning(f"MARKET SELL: {symbol} qty={quantity}")
        return order
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"MARKET SELL failed {symbol}: {e}")
        return None


def get_order_status(symbol: str, order_id: str) -> dict | None:
    try:
        return get_client().get_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {e}")
    return None


def transfer_to_funding(asset: str, amount: float) -> bool:
    """Transfer asset from Spot to Funding wallet via Universal Transfer API."""
    try:
        result = get_client().make_universal_transfer(
            from_account_type="SPOT",
            to_account_type="FUNDING",
            asset=asset,
            amount=amount,
        )
        txn_id = result.get("tranId", "?")
        logger.info(f"TRANSFER: {amount:.8f} {asset} Spot → Funding (id={txn_id})")
        return True
    except Exception as e:
        logger.error(f"TRANSFER failed {asset}: {e}")
        return False

def place_oco_sell(symbol: str, quantity: float, take_profit_price: float, stop_loss_price: float) -> dict | None:
    """Place OCO sell order: limit take-profit + stop-loss-limit.
    If OCO fails, falls back to stop-loss-limit only."""
    try:
        from binance.enums import SIDE_SELL
        # Use create_oco_order for spot
        order = get_client().create_oco_order(
            symbol=symbol,
            side=SIDE_SELL,
            quantity=quantity,
            price=round(take_profit_price, 2),
            stopPrice=round(stop_loss_price, 2),
            stopLimitPrice=round(stop_loss_price * 0.999, 2),
            stopLimitTimeInForce="GTC",
        )
        logger.info(f"OCO SELL: {symbol} qty={quantity} TP={take_profit_price} SL={stop_loss_price}")
        return order
    except Exception as e:
        logger.error(f"OCO failed for {symbol}: {e}, trying stop-loss only")
        try:
            order = get_client().create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                quantity=quantity,
                price=round(stop_loss_price * 0.999, 2),
                stopPrice=round(stop_loss_price, 2),
                timeInForce="GTC",
            )
            logger.info(f"STOP LOSS: {symbol} qty={quantity} SL={stop_loss_price}")
            return order
        except Exception as e2:
            logger.error(f"Stop-loss failed for {symbol}: {e2}")
            return None
