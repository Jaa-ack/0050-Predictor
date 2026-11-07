"""Causal own-price direction distributions; no target fitting or calibration."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import _timestamps
from .universe import TRAINING_TICKERS

PRICE_PATH_FEATURES = tuple(
    f'f_path_{stat}_{horizon}_{window}'
    for horizon in (1, 5, 20) for window in (60, 252) for stat in ('up', 'flat')
) + ('f_path_same_sign_1_60',)


def add_price_path_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Append 13 rolling statistics on the trusted primary-market calendar.

    Return horizons end at the known price anchor, never at a future label.
    Windows of 60/252 sessions require 30/126 finite observations respectively.
    Sign agreement uses 30 valid consecutive pairs within 60 sessions, counting
    flat as a distinct sign. Each asset supplies only its own historical prices.
    """
    out = frame.copy()
    dates = pd.to_datetime(frame.date).astype('datetime64[ns]')
    decisions = _timestamps(frame.decision_ts, 'decision_ts', allow_missing=True)
    receipt = (dates.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
    if (decisions.notna() & decisions.lt(receipt)).any():
        raise ValueError('Price path requires receipt by anchor-date 20:00')
    if set(PRICE_PATH_FEATURES).intersection(frame.columns):
        raise ValueError('Price path features already exist')
    work = frame[['ticker', 'f_return_1', 'f_return_5', 'f_return_20']].reset_index(drop=True).copy()
    work['date'] = dates.to_numpy()
    if work.duplicated(['date', 'ticker']).any():
        raise ValueError('Price path requires unique stock/session rows')
    calendar = pd.DatetimeIndex(dates.loc[frame.ticker.isin((*TRAINING_TICKERS, '0050'))].unique()).sort_values()
    result = {column: np.full(len(frame), np.nan) for column in PRICE_PATH_FEATURES}
    for positions in work.groupby('ticker', sort=False).indices.values():
        stock = work.iloc[positions].set_index('date').reindex(calendar)
        requested = work.iloc[positions].date
        for horizon in (1, 5, 20):
            values = pd.to_numeric(stock[f'f_return_{horizon}'], errors='coerce').replace([np.inf, -np.inf], np.nan)
            for stat, predicate in [('up', values.gt(0)), ('flat', values.eq(0))]:
                indicator = predicate.astype(float).where(values.notna())
                for window, minimum in [(60, 30), (252, 126)]:
                    fraction = indicator.rolling(window, min_periods=minimum).mean()
                    result[f'f_path_{stat}_{horizon}_{window}'][positions] = fraction.reindex(requested).to_numpy()
            if horizon == 1:
                sign = np.sign(values)
                agreement = sign.eq(sign.shift(1)).astype(float).where(values.notna() & values.shift(1).notna())
                fraction = agreement.rolling(60, min_periods=30).mean()
                result['f_path_same_sign_1_60'][positions] = fraction.reindex(requested).to_numpy()
    for column, values in result.items():
        values[decisions.isna().to_numpy()] = np.nan
        out[column] = values
    return out
