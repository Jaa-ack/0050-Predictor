"""Isolated Alpha158-inspired formulas and prefix-recalculation audits.

Method references, pinned versions and licenses: docs/OPEN_SOURCE_RESEARCH.md.
These are new pandas implementations, not the Qlib expression engine or its
full Alpha158 dataset. All windows count observed market sessions, including
missing security bars. Only already-visible rows from build_features enter.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import get_feature_columns

ALPHA_COLUMNS = tuple('f_oss_' + name for name in (
    'body_range', 'upper_wick', 'lower_wick', 'slope20', 'rsqr20',
    'resi20', 'corr20', 'sump20', 'wvma20'))


def _trend(values, statistic):
    x = np.arange(len(values), dtype=float)
    x -= x.mean()
    centered = values - values.mean()
    slope = x @ centered / (x @ x)
    if statistic == 'slope':
        return slope / values[-1]
    if statistic == 'resi':
        return (centered[-1] - slope * x[-1]) / values[-1]
    variance = centered @ centered
    return (slope * slope * (x @ x) / variance) if variance > 0 else np.nan


def add_alpha_subset(frame, calendar):
    """Preserve all rows/metadata; add nine dimensionless, past-only factors.

    Prices use the same adjustment factor as the official feature builder.
    Volume is vendor share volume; split-consistent historical volume remains
    a data-quality limitation. Zero ranges/activity stay unknown, not infinity.
    """
    out = frame.copy()
    if set(ALPHA_COLUMNS) & set(out):
        raise ValueError('Alpha subset already present')
    if not out.index.is_unique or out.duplicated(['ticker', 'date']).any():
        raise ValueError('Unique feature rows required')
    calendar = pd.DatetimeIndex(calendar).sort_values()
    if not calendar.is_unique or not set(out.date) <= set(calendar):
        raise ValueError('Calendar must contain all feature dates')
    for column in ALPHA_COLUMNS:
        out[column] = np.nan
    for _, rows in frame.groupby('ticker', sort=True):
        bars = rows.set_index('date').reindex(calendar)
        factor = bars.adj_close / bars.close
        close, opened = bars.adj_close, bars.open * factor
        high, low = bars.high * factor, bars.low * factor
        spread = (high - low).replace(0, np.nan)
        delta = close.diff()
        activity = close.pct_change(fill_method=None).abs() * bars.volume
        values = pd.DataFrame(index=calendar)
        values[ALPHA_COLUMNS[0]] = (close - opened) / spread
        values[ALPHA_COLUMNS[1]] = (high - pd.concat([opened, close], axis=1).max(axis=1)) / spread
        values[ALPHA_COLUMNS[2]] = (pd.concat([opened, close], axis=1).min(axis=1) - low) / spread
        for column, statistic in zip(ALPHA_COLUMNS[3:6], ('slope', 'rsqr', 'resi')):
            values[column] = close.rolling(20, min_periods=20).apply(
                lambda v: _trend(v, statistic), raw=True)
        values[ALPHA_COLUMNS[6]] = close.rolling(20, min_periods=20).corr(np.log1p(bars.volume))
        values[ALPHA_COLUMNS[7]] = (delta.clip(lower=0).rolling(20, min_periods=20).sum()
            / delta.abs().rolling(20, min_periods=20).sum().replace(0, np.nan))
        values[ALPHA_COLUMNS[8]] = (activity.rolling(20, min_periods=20).std()
            / activity.rolling(20, min_periods=20).mean().replace(0, np.nan))
        out.loc[rows.index, list(ALPHA_COLUMNS)] = values.reindex(rows.date).to_numpy()
    out[list(ALPHA_COLUMNS)] = out[list(ALPHA_COLUMNS)].replace([np.inf, -np.inf], np.nan)
    return out


def causal_feature_audit(prices, builder, cutoffs):
    """Compare complete history with independent prefixes; never compare labels.

    Detects changed values, changed NaN masks and missing decision rows. This
    samples specified cutoffs; passing cannot prove all possible leakage absent.
    A deliberately leaky negative control belongs in the tests, not real inputs.
    """
    if not len(cutoffs):
        raise ValueError('At least one prefix cutoff required')
    full = builder(prices).set_index(['ticker', 'date']).sort_index()
    columns = get_feature_columns(full)
    changed, checks = set(), []
    comparisons = 0
    for cutoff in cutoffs:
        cutoff = pd.Timestamp(cutoff)
        prefix = builder(prices.loc[prices.date.le(cutoff)].copy()).set_index(['ticker', 'date']).sort_index()
        expected = full.loc[full.index.get_level_values('date') <= cutoff]
        same_rows = expected.index.equals(prefix.index)
        if set(columns) != set(get_feature_columns(prefix)):
            raise ValueError('Feature schema changed with prefix length')
        actual = prefix.reindex(expected.index)[columns].to_numpy(dtype=float)
        reference = expected[columns].to_numpy(dtype=float)
        equal = np.isclose(actual, reference, rtol=1e-10, atol=1e-12, equal_nan=True)
        changed.update(np.asarray(columns)[~equal.all(axis=0)].tolist())
        comparisons += equal.size
        checks.append(dict(cutoff=str(cutoff.date()), rows=len(expected), same_rows=same_rows,
                           differing_cells=int((~equal).sum())))
    return dict(passed=not changed and all(c['same_rows'] for c in checks),
        changed_features=sorted(changed), comparisons=comparisons, checks=checks,
        limitation='Sampled postclose feature-prefix consistency, not proof of observed PIT delivery or model-level causality.')
