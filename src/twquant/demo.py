"""Seeded synthetic data for exercising the pipeline, never feasibility evidence."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import validate_prices

DEMO_TICKERS = ("0050", "2330", "2454", "2317", "2308", "3711", "1303", "2383", "2881", "2891", "3037")


def make_demo_prices(sessions: int = 800, seed: int = 42) -> pd.DataFrame:
    """Synthetic weekday sessions ending 2025-12-31; no real TWSE observations."""
    if not isinstance(sessions, int) or sessions < 1:
        raise ValueError("sessions must be a positive integer")
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2025-12-31", periods=sessions)
    market_noise = rng.normal(0.0002, 0.009, sessions)
    slow_cycle = 0.0015 * np.sin(np.arange(sessions) / 28)
    market_returns = market_noise + slow_cycle
    frames = []
    for index, ticker in enumerate(DEMO_TICKERS):
        if ticker == "0050":
            returns = market_returns
            initial = 60.0
        else:
            beta = 0.7 + 0.07 * index
            returns = beta * market_returns + rng.normal(0, 0.008, sessions)
            initial = 50 + 55 * index
        close = initial * np.exp(np.cumsum(returns))
        previous = np.r_[initial, close[:-1]]
        opened = previous * np.exp(rng.normal(0, 0.0025, sessions))
        upper = rng.uniform(0.001, 0.012, sessions)
        lower = rng.uniform(0.001, 0.012, sessions)
        volume = np.maximum(1000, rng.lognormal(16, 0.4, sessions)).astype(np.int64)
        frame = pd.DataFrame({
            "ticker": ticker, "date": dates,
            "open": opened,
            "high": np.maximum(opened, close) * (1 + upper),
            "low": np.minimum(opened, close) * (1 - lower),
            "close": close, "adj_close": close, "volume": volume,
            "available_at": (dates.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).tz_convert("UTC"),
            "source": "synthetic_demo", "adjustment_source": "synthetic_no_corporate_actions",
            "availability_basis": "synthetic_calendar",
        })
        frames.append(frame)
    return validate_prices(pd.concat(frames, ignore_index=True))
