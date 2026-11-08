"""Own-stock institutional statistics under explicit, unverified receipt lags."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import _timestamps
from .universe import TRAINING_TICKERS

FLOW_FAMILIES = (('foreign', 'Foreign_Investor'), ('trust', 'Investment_Trust'))
FLOW_FEATURES = tuple(f'f_flow_{family}_{stat}' for family, _ in FLOW_FAMILIES
                      for stat in ('imbalance', 'mean5', 'mean20', 'active'))


def add_institutional_flow_features(frame: pd.DataFrame, history: pd.DataFrame, *, lag_sessions=2) -> pd.DataFrame:
    """Append eight own-stock features; missing reports never carry forward.

    A source observation is assumed received at 20:00 after the declared number
    of primary-market sessions. It expires at the next observed session's 08:30
    cutoff, inclusive. This contract does not establish historical publication
    or finalization. Trailing means require every observation in their window.
    """
    if isinstance(lag_sessions, bool) or not isinstance(lag_sessions, int) or not 0 <= lag_sessions <= 5:
        raise ValueError('Flow lag must be an integer from zero to five sessions')
    required = {'date', 'stock_id', 'name', 'buy', 'sell', 'source'}
    if not required.issubset(history) or history.empty:
        raise ValueError('Flow history requires source-tagged daily category quantities')
    data = history[sorted(required)].copy()
    if not data.source.isin(('finmind_historical_institutional', 'synthetic')).all():
        raise ValueError('Flow history requires an explicit supported source')
    if not data.stock_id.map(lambda x: isinstance(x, str)).all() or not data.stock_id.str.fullmatch(r'[0-9]{4,6}').all():
        raise ValueError('Flow history requires explicit string stock IDs')
    data['date'] = pd.to_datetime(data.date, errors='raise')
    if data.date.dt.tz is not None or data.date.isna().any() or not data.date.eq(data.date.dt.normalize()).all():
        raise ValueError('Flow history requires naive daily dates')
    data['date'] = data.date.astype('datetime64[ns]')
    if data.duplicated(['date', 'stock_id', 'name']).any():
        raise ValueError('Duplicate flow stock/category/date')
    for column in ('buy', 'sell'):
        data[column] = pd.to_numeric(data[column], errors='raise').astype(float)
    if not np.isfinite(data[['buy', 'sell']]).all().all() or not data[['buy', 'sell']].ge(0).all().all():
        raise ValueError('Flow quantities must be finite and nonnegative')
    if set(FLOW_FEATURES).intersection(frame.columns):
        raise ValueError('Flow features already exist')
    out = frame.copy()
    dates = pd.to_datetime(frame.date).astype('datetime64[ns]')
    decisions = _timestamps(frame.decision_ts, 'decision_ts', allow_missing=True)
    receipt = (dates.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
    if (decisions.notna() & decisions.lt(receipt)).any():
        raise ValueError('Flow decisions cannot precede anchor-date 20:00')
    calendar = pd.DatetimeIndex(dates.loc[frame.ticker.isin((*TRAINING_TICKERS, '0050'))].unique()).sort_values()
    next_date = pd.Series(calendar, index=calendar).shift(-1)
    expiry = (pd.to_datetime(dates.map(next_date)).dt.tz_localize('Asia/Taipei')
              + pd.Timedelta(hours=8, minutes=30)).dt.tz_convert('UTC')
    valid = decisions.notna() & dates.isin(calendar) & (
        decisions.le(expiry) | (expiry.isna() & decisions.eq(receipt)))
    work = pd.DataFrame({'date': dates.to_numpy(), 'ticker': frame.ticker.to_numpy()})
    if work.duplicated(['date', 'ticker']).any():
        raise ValueError('Flow features require unique stock/session rows')
    selected = data.loc[data.name.isin([name for _, name in FLOW_FAMILIES])]
    groups = {key: group.set_index('date') for key, group in selected.groupby(['stock_id', 'name'])}
    result = {column: np.full(len(frame), np.nan) for column in FLOW_FEATURES}
    for positions in work.groupby('ticker', sort=False).indices.values():
        ticker = work.iloc[positions[0]].ticker
        requested = work.iloc[positions].date
        for family, name in FLOW_FAMILIES:
            source = groups.get((ticker, name))
            if source is None:
                continue
            source = source.reindex(calendar)
            gross = source.buy + source.sell
            if np.isinf(gross).any():
                raise ValueError('Flow gross quantities must be finite')
            known = source.buy.notna() & source.sell.notna()
            imbalance = ((source.buy-source.sell) / gross.where(gross.gt(0))).where(gross.ne(0), 0.).where(known)
            stats = {'imbalance': imbalance, 'mean5': imbalance.rolling(5, min_periods=5).mean(),
                     'mean20': imbalance.rolling(20, min_periods=20).mean(),
                     'active': gross.gt(0).astype(float).where(known)}
            for stat, values in stats.items():
                result[f'f_flow_{family}_{stat}'][positions] = values.shift(lag_sessions).reindex(requested).to_numpy()
    for column, values in result.items():
        values[~valid.to_numpy()] = np.nan
        out[column] = values
    out.attrs['flow_receipt_contract'] = dict(lag_sessions=lag_sessions,
        assumed_receipt='20:00 Asia/Taipei on lagged primary session',
        expiry='next observed primary session 08:30 inclusive', original_pit_verified=False)
    return out
