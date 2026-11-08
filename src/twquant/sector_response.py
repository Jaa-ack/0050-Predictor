"""Past-known own-stock sector exposures and current preopen interactions."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import _timestamps
from .universe import TRAINING_TICKERS

RESPONSE_SYMBOLS=('xlf','xlp','xlc','xli','xlb','xle','smh')
RESPONSE_FEATURES=tuple(f'f_response_{s}_{stat}' for s in RESPONSE_SYMBOLS
                        for stat in ('beta60','beta_window_return'))
RESPONSE_CONTRACT=dict(window_sessions=60,minimum_pairs=30,beta_clip=[-3.,3.],
    alignment='previous_primary_anchor_us_window',decision_clock='next_primary08:30',
    original_pit_verified=False)


def add_sector_response_features(frame):
    """Pair completed Taiwan returns with the preceding preopen US window.

    The current preopen shock never enters its own exposure estimate. Both
    covariance and variance use the same trailing paired observations. Global
    context/calendar come only from fixed-ten/0050 rows; external stocks use
    their own past-known returns, never another external stock or target.
    """
    inputs=[f'f_ext_us_sector_{s}_window_return' for s in RESPONSE_SYMBOLS]
    if not {'ticker','date','decision_ts','f_return_1',*inputs}.issubset(frame):
        raise ValueError('Sector response requires known returns and all seven sector windows')
    if frame.duplicated(['ticker','date']).any() or set(RESPONSE_FEATURES).intersection(frame):
        raise ValueError('Sector response requires unique unaugmented stock/session rows')
    dates=pd.to_datetime(frame.date)
    if dates.dt.tz is not None or dates.isna().any() or not dates.eq(dates.dt.normalize()).all():
        raise ValueError('Sector response requires naive daily dates')
    decisions=_timestamps(frame.decision_ts,'sector response decision_ts',allow_missing=True)
    local=decisions.dt.tz_convert('Asia/Taipei')
    primary=frame.ticker.isin((*TRAINING_TICKERS,'0050'))
    calendar=pd.DatetimeIndex(dates.loc[primary].unique()).sort_values()
    if calendar.empty:raise ValueError('Sector response requires the primary market calendar')
    local_date=local.dt.tz_localize(None).dt.normalize()
    next_dates=dates.map(pd.Series(calendar,index=calendar).shift(-1))
    clock=(local.dt.hour.eq(8)&local.dt.minute.eq(30)&local.dt.second.eq(0)
           &local.dt.microsecond.eq(0)&local.dt.nanosecond.eq(0)&local_date.gt(dates))
    eligible=decisions.notna()&dates.isin(calendar)
    if not (clock&local_date.eq(next_dates)).loc[eligible].all():
        raise ValueError('Sector response requires next observed primary session08:30')
    numeric=frame[['f_return_1',*inputs]].apply(pd.to_numeric,errors='raise').astype(float)
    if np.isinf(numeric).any().any():raise ValueError('Sector response inputs cannot be infinite')
    context=numeric.loc[primary,inputs].copy();context['date']=dates.loc[primary]
    if context.groupby('date')[inputs].nunique(dropna=False).gt(1).any().any():
        raise ValueError('Sector response context must be uniform across primary rows')
    global_window=context.groupby('date')[inputs].first().reindex(calendar)
    work=pd.DataFrame(dict(date=dates.to_numpy(),ticker=frame.ticker.to_numpy(),known_return=numeric.f_return_1.to_numpy()))
    values={c:np.full(len(frame),np.nan) for c in RESPONSE_FEATURES}
    for positions in work.groupby('ticker',sort=False).indices.values():
        stock=work.iloc[positions].set_index('date').known_return.reindex(calendar)
        requested=work.iloc[positions].date
        for symbol,column in zip(RESPONSE_SYMBOLS,inputs,strict=True):
            prior=global_window[column].shift(1)
            paired=stock.notna()&prior.notna()
            x,y=prior.where(paired),stock.where(paired)
            variance=x.rolling(60,min_periods=30).var()
            beta=(y.rolling(60,min_periods=30).cov(x)/variance.where(variance.gt(1e-12))).clip(-3.,3.)
            interaction=beta*global_window[column]
            values[f'f_response_{symbol}_beta60'][positions]=beta.reindex(requested).to_numpy()
            values[f'f_response_{symbol}_beta_window_return'][positions]=interaction.reindex(requested).to_numpy()
    out=frame.copy()
    for column,array in values.items():
        array[~eligible.to_numpy()]=np.nan
        out[column]=array
    out.attrs['sector_response_contract']={**RESPONSE_CONTRACT,'beta_clip':list(RESPONSE_CONTRACT['beta_clip'])}
    return out
