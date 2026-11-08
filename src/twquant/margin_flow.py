"""Own-stock financing and margin-short ratios with explicit receipt assumptions."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import _timestamps
from .universe import TRAINING_TICKERS

MARGIN_FAMILIES = (('finance', 'MarginPurchase', 1.), ('short', 'ShortSale', -1.))
MARGIN_STATS = ('imbalance', 'mean5', 'mean20', 'active', 'utilization',
                'balance_change', 'repayment_share', 'limit_positive', 'balance_change_mean20')
MARGIN_FEATURES = tuple(f'f_margin_{family}_{stat}' for family, _, _ in MARGIN_FAMILIES for stat in MARGIN_STATS)
MARGIN_QUANTITIES = tuple(prefix+field for _, prefix, _ in MARGIN_FAMILIES
                          for field in ('Buy', 'Sell', 'CashRepayment', 'Limit', 'TodayBalance', 'YesterdayBalance')) + ('OffsetLoanAndShort',)


def add_margin_features(frame, history, *, lag_sessions=2):
    """Append18 ratios after an assumed lagged-session21:00 receipt.

    Expiry is next observed primary session08:30 inclusive. Missing daily
    reports never carry; explicit zero activity is neutral, while utilization
    at a zero limit is unknown. A positive limit is not a trading permission.
    No share-volume normalization or repaired balance identity is assumed.
    """
    if isinstance(lag_sessions, bool) or not isinstance(lag_sessions, int) or not 0 <= lag_sessions <= 5:
        raise ValueError('Margin lag must be an integer from zero to five sessions')
    required = {'date', 'stock_id', 'source', *MARGIN_QUANTITIES}
    if history.empty or not required.issubset(history):
        raise ValueError('Margin history requires source-tagged daily quantities')
    data = history[sorted(required)].copy()
    if not data.source.isin(('finmind_historical_margin_short', 'synthetic')).all():
        raise ValueError('Margin history requires an explicit supported source')
    if not data.stock_id.map(lambda x: isinstance(x, str)).all() or not data.stock_id.str.fullmatch(r'[0-9]{4,6}').all():
        raise ValueError('Margin history requires explicit string stock IDs')
    data['date'] = pd.to_datetime(data.date, errors='raise')
    if data.date.dt.tz is not None or data.date.isna().any() or not data.date.eq(data.date.dt.normalize()).all():
        raise ValueError('Margin history requires naive daily dates')
    data['date'] = data.date.astype('datetime64[ns]')
    if data.duplicated(['date', 'stock_id']).any():
        raise ValueError('Duplicate margin stock/date')
    for column in MARGIN_QUANTITIES:
        data[column] = pd.to_numeric(data[column], errors='raise').astype(float)
    if not np.isfinite(data[list(MARGIN_QUANTITIES)]).all().all() or not data[list(MARGIN_QUANTITIES)].ge(0).all().all():
        raise ValueError('Margin quantities must be finite and nonnegative')
    if set(MARGIN_FEATURES).intersection(frame):
        raise ValueError('Margin features already exist')
    dates = pd.to_datetime(frame.date).astype('datetime64[ns]')
    decisions = _timestamps(frame.decision_ts, 'decision_ts', allow_missing=True)
    receipt = (dates.dt.tz_localize('Asia/Taipei')+pd.Timedelta(hours=21)).dt.tz_convert('UTC')
    if (decisions.notna() & decisions.lt(receipt)).any():
        raise ValueError('Margin decisions cannot precede anchor-date21:00')
    calendar = pd.DatetimeIndex(dates.loc[frame.ticker.isin((*TRAINING_TICKERS, '0050'))].unique()).sort_values()
    next_date = pd.Series(calendar, index=calendar).shift(-1)
    expiry = (pd.to_datetime(dates.map(next_date)).dt.tz_localize('Asia/Taipei')
              + pd.Timedelta(hours=8, minutes=30)).dt.tz_convert('UTC')
    valid = decisions.notna() & dates.isin(calendar) & (
        decisions.le(expiry) | (expiry.isna() & decisions.eq(receipt)))
    work = pd.DataFrame(dict(date=dates.to_numpy(), ticker=frame.ticker.to_numpy()))
    if work.duplicated(['date', 'ticker']).any():
        raise ValueError('Margin features require unique stock/session rows')
    groups = {ticker: group.set_index('date') for ticker, group in data.groupby('stock_id')}
    result = {column: np.full(len(frame), np.nan) for column in MARGIN_FEATURES}
    for positions in work.groupby('ticker', sort=False).indices.values():
        ticker = work.iloc[positions[0]].ticker
        if ticker not in groups: continue
        source = groups[ticker].reindex(calendar)
        requested = work.iloc[positions].date
        for family, prefix, sign in MARGIN_FAMILIES:
            buy, sell, cash = (source[prefix+field] for field in ('Buy', 'Sell', 'CashRepayment'))
            today, yesterday, limit = (source[prefix+field] for field in ('TodayBalance', 'YesterdayBalance', 'Limit'))
            gross, balances = buy+sell, today+yesterday
            if np.isinf(pd.concat([gross, balances, gross+cash])).any():
                raise ValueError('Margin aggregate quantities must be finite')
            known = buy.notna()
            ratio = lambda numerator, denominator: (numerator/denominator.where(denominator.gt(0))).where(denominator.ne(0),0.).where(known)
            imbalance = ratio(sign*(buy-sell), gross)
            change = ratio(today-yesterday, balances)
            stats = dict(imbalance=imbalance, mean5=imbalance.rolling(5,min_periods=5).mean(),
                mean20=imbalance.rolling(20,min_periods=20).mean(), active=gross.gt(0).astype(float).where(known),
                utilization=(today/limit.where(limit.gt(0))).where(known), balance_change=change,
                repayment_share=ratio(cash,gross+cash), limit_positive=limit.gt(0).astype(float).where(known),
                balance_change_mean20=change.rolling(20,min_periods=20).mean())
            for stat, values in stats.items():
                result[f'f_margin_{family}_{stat}'][positions] = values.shift(lag_sessions).reindex(requested).to_numpy()
    out = frame.copy()
    for column, values in result.items():
        values[~valid.to_numpy()] = np.nan
        out[column] = values
    out.attrs['margin_receipt_contract'] = dict(lag_sessions=lag_sessions,
        assumed_receipt='21:00 Asia/Taipei on lagged primary session',
        expiry='next observed primary session08:30 inclusive', original_pit_verified=False,
        evidence_kind='synthetic' if data.source.eq('synthetic').any() else 'historical')
    return out
