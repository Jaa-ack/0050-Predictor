"""Causal daily adaptations of day-trader-skill scan.py::find_setups.

Copyright (c) 2026 Jake Schincariol. MIT; see docs/licenses/TRADER_SKILLS_MIT.txt.
Upstream commit: 3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd.
Existing RSI initialization/units are retained; no US data or session logic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


SIGNAL_COLUMNS = ('f_scan_breakout_20d', 'f_scan_bull_flag', 'f_scan_reclaim_ma20',
                  'f_scan_oversold', 'f_scan_oversold_bounce')
SIGNAL_VERSION = 'daily-close-setups-v1-existing-rsi01-full-session-windows'


def add_daily_signals(frame, calendar_dates):
    """Append five 0/1 rules to a trusted build_features frame; unknown stays NaN.

    Calendar windows retain suspended/missing/late bars. All inputs must already
    be visible at their own decision; late records are masked again defensively.
    No outcome, peer, volume or external asset is used to compute a stock's rule.
    """
    required = {'ticker', 'date', 'adj_close', 'f_rsi_14', 'available_at', 'decision_ts'}
    if not required <= set(frame):
        raise ValueError('Daily signals need a point-in-time feature frame')
    calendar = pd.DatetimeIndex(calendar_dates)
    if (calendar.hasnans or calendar.tz is not None or not calendar.is_unique
            or not calendar.is_monotonic_increasing or not calendar.equals(calendar.normalize())
            or not set(frame.date) <= set(calendar)):
        raise ValueError('Signal calendar must be sorted, unique, naive market-session dates')
    if not frame.index.is_unique or frame.duplicated(['ticker', 'date']).any():
        raise ValueError('Daily signals reject duplicate ticker/date or index')
    if any(not g.date.is_monotonic_increasing for _, g in frame.groupby('ticker')):
        raise ValueError('Daily signals require date-sorted rows within each ticker')
    out = frame.copy()
    for column in SIGNAL_COLUMNS:
        out[column] = np.nan
    for _, group in frame.groupby('ticker', sort=False):
        data = group.set_index('date').reindex(calendar)
        for col in ('available_at', 'decision_ts'):
            if any(pd.Timestamp(v).tzinfo is None for v in data[col].dropna()):
                raise ValueError('Signal timestamps require explicit timezones')
        known = pd.to_datetime(data.available_at, utc=True).le(pd.to_datetime(data.decision_ts, utc=True))
        c = pd.to_numeric(data.adj_close, errors='raise').where(known)
        c = c.where(np.isfinite(c) & c.gt(0))
        rsi = pd.to_numeric(data.f_rsi_14, errors='raise').where(known & c.notna())
        rsi = rsi.where(rsi.between(0, 1))
        prior_high = c.shift(1).rolling(20, min_periods=20).max()
        ma = c.rolling(20, min_periods=20).mean()
        with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
            run = (c.shift(5) / c.shift(15) - 1).replace([np.inf, -np.inf], np.nan)
            range5 = ((c.rolling(5).max() - c.rolling(5).min()) / c).replace([np.inf, -np.inf], np.nan)
        complete15 = c.rolling(15).count().ge(15)
        complete16 = c.rolling(16).count().ge(16)
        values = {
            'f_scan_breakout_20d': c.gt(prior_high).astype(float).where(c.notna() & prior_high.notna()),
            'f_scan_bull_flag': (run.gt(.10) & range5.lt(.05)).astype(float).where(complete16 & run.notna() & range5.notna()),
            'f_scan_reclaim_ma20': (c.shift(1).lt(ma.shift(1)) & c.gt(ma)).astype(float).where(ma.notna() & ma.shift(1).notna()),
            'f_scan_oversold': rsi.lt(.30).astype(float).where(rsi.notna() & complete15),
            'f_scan_oversold_bounce': (rsi.shift(1).lt(.30) & c.gt(c.shift(1))).astype(float).where(rsi.shift(1).notna() & complete16),
        }
        for column, value in values.items():
            out.loc[group.index, column] = value.reindex(group.date).to_numpy()
    return out
