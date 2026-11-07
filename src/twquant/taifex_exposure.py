"""Past-only stock responses to regular-session futures and known night moves."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .taifex_history import CONTRACTS
from .universe import TRAINING_TICKERS


def _regular_returns(history: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    regular = history.loc[history.session.eq('regular') & history.close.gt(0) & history.volume.gt(0)].copy()
    regular['trade_date'] = pd.to_datetime(regular.trade_date).astype('datetime64[ns]')
    regular['expiry'] = regular.expiry.astype(str)
    regular = regular.loc[regular.expiry.ge(regular.trade_date.dt.strftime('%Y%m'))]
    front = (regular.sort_values(['trade_date', 'contract', 'expiry'])
             .drop_duplicates(['trade_date', 'contract']))
    result = pd.DataFrame(index=calendar)
    previous = pd.Series(calendar, index=calendar).shift(1)
    for code in CONTRACTS:
        quotes = regular.loc[regular.contract.eq(code), ['trade_date', 'expiry', 'close']]
        current = front.loc[front.contract.eq(code), ['trade_date', 'expiry', 'close']]
        if current.empty:
            result[code] = np.nan
            continue
        reference = quotes.rename(columns={'trade_date': 'reference_date', 'close': 'reference_close'})
        paired = pd.merge_asof(current.sort_values('trade_date'), reference.sort_values('reference_date'),
            left_on='trade_date', right_on='reference_date', by='expiry',
            direction='backward', allow_exact_matches=False).set_index('trade_date').reindex(calendar)
        # Missing sessions and contract rolls must not turn a multi-session move
        # into the daily explanatory return paired with the stock's daily return.
        result[code] = (paired.close / paired.reference_close - 1).where(paired.reference_date.eq(previous))
    return result


def add_taifex_exposure_features(frame: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Add 60-session (minimum 30 pairs) clipped exposures and night interactions.

    Regular quotes on the price-anchor date are assumed received by 20:00.
    History must be normalized by ``parse_taifex_history``. Each stock uses only
    its own past returns; evaluation assets cannot supply calendar or peers.
    Unknown exposure, night signal, or decision remains unknown.
    """
    out = frame.copy()
    dates = pd.to_datetime(frame.date).astype('datetime64[ns]')
    decisions = pd.to_datetime(frame.decision_ts, utc=True)
    receipt = (dates.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
    if (decisions.notna() & decisions.lt(receipt)).any():
        raise ValueError('TAIFEX regular exposure requires receipt by anchor-date 20:00')
    calendar = pd.DatetimeIndex(dates.loc[frame.ticker.isin((*TRAINING_TICKERS, '0050'))].unique()).sort_values()
    regular = _regular_returns(history, calendar)
    work = pd.DataFrame({'date': dates.to_numpy(), 'ticker': frame.ticker.to_numpy(),
        'return': pd.to_numeric(frame.f_return_1, errors='coerce').replace([np.inf, -np.inf], np.nan).to_numpy()})
    if work.duplicated(['date', 'ticker']).any():
        raise ValueError('TAIFEX exposure requires unique stock/session rows')
    for code in CONTRACTS:
        prefix = 'f_ext_taifex_' + code.lower()
        beta = np.full(len(frame), np.nan)
        for positions in work.groupby('ticker', sort=False).indices.values():
            stock = work.iloc[positions].set_index('date')['return'].reindex(calendar)
            sector = regular[code].where(stock.notna())
            covariance = stock.rolling(60, min_periods=30).cov(sector)
            variance = sector.rolling(60, min_periods=30).var()
            exposure = (covariance / variance.where(variance.gt(1e-12))).clip(-3., 3.)
            beta[positions] = exposure.reindex(work.iloc[positions].date).to_numpy()
        beta[decisions.isna().to_numpy()] = np.nan
        out[prefix + '_beta60'] = beta
        night = pd.to_numeric(frame.get(prefix + '_night_return', pd.Series(np.nan, index=frame.index)), errors='coerce')
        out[prefix + '_beta_night_return'] = beta * night.replace([np.inf, -np.inf], np.nan).to_numpy()
    return out
