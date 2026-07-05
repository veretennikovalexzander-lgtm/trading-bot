"""
Abstract base class for trading strategies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategySignal:
    signal: Signal
    symbol: str
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""


class BaseStrategy(ABC):
    """All strategies must implement analyze()."""

    def __init__(self, symbol: str, interval: str = "5m"):
        self.symbol = symbol
        self.interval = interval

    @abstractmethod
    def analyze(self, df) -> StrategySignal:
        """
        Analyze OHLCV DataFrame and return a trading signal.
        df columns: open, high, low, close, volume
        """
        ...
