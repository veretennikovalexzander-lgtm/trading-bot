"""
Pytest fixtures — no .env loading for test isolation.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Block .env loading during tests
os.environ["DOTENV_TEST_MODE"] = "1"


@pytest.fixture(autouse=True)
def clean_env():
    for key in list(os.environ.keys()):
        if (
            key.startswith("BOT_")
            or key.startswith("POSTGRES_")
            or key.startswith("BINANCE_")
        ):
            del os.environ[key]
    import src.config as cfg

    cfg._config = None
    yield
    cfg._config = None


@pytest.fixture
def fresh_rm():
    import src.risk_manager as rm_module

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        temp_path = Path(f.name)
    old = rm_module.STATE_FILE
    rm_module.STATE_FILE = temp_path
    from src.risk_manager import RiskManager

    rm = RiskManager()
    yield rm
    if temp_path.exists():
        temp_path.unlink()
    rm_module.STATE_FILE = old


@pytest.fixture
def sample_ohlcv_data():
    import numpy as np
    import pandas as pd

    np.random.seed(42)
    n = 50
    base = 62000
    closes = base + np.cumsum(np.random.randn(n) * 20)
    highs = closes + np.abs(np.random.randn(n) * 10)
    lows = closes - np.abs(np.random.randn(n) * 10)
    opens = closes + np.random.randn(n) * 5
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.abs(np.random.randn(n) * 100),
        }
    )


@pytest.fixture
def oversold_ohlcv_data():
    """Very aggressive crash to force price below lower BB and RSI < 30."""
    import numpy as np
    import pandas as pd

    np.random.seed(123)
    n = 60
    base = 62000
    # 8% crash over 60 candles
    trend = np.linspace(0, -5000, n)
    closes = closes = np.concatenate([base + np.random.randn(50) * 20, base - 6000 + np.linspace(0, -300, 10) + np.random.randn(10) * 5])
    highs = closes + np.abs(np.random.randn(n) * 5)
    lows = closes - np.abs(np.random.randn(n) * 5)
    opens = closes + np.random.randn(n) * 2
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.abs(np.random.randn(n) * 100),
        }
    )


@pytest.fixture
def in_memory_db():
    import src.models  # noqa
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.database import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
