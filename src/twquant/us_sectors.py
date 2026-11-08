"""Offline sector-price context with explicit source and assumed US receipts."""
from __future__ import annotations

import numpy as np
import pandas as pd

SECTOR_SYMBOLS = ('XLF', 'XLP', 'XLC', 'XLI', 'XLB', 'XLE', 'SMH')


def sector_price_events(history: pd.DataFrame) -> pd.DataFrame:
    """Convert all seven source-tagged adjusted histories into global events.

    The receipt is assumed at 17:00 New York local time, never original PIT
    evidence. Return1 uses adjacent observed sector-market sessions; missing
    individual quotes break the pair. No prices are filled before inception.
    """
    required = {'date', 'symbol', 'adj_close', 'source'}
    if not required.issubset(history.columns):
        raise ValueError('Sector history requires date, symbol, adj_close and source')
    data = history[list(sorted(required))].copy()
    if set(data.symbol) != set(SECTOR_SYMBOLS):
        raise ValueError('Sector history requires the seven declared US symbols')
    if not data.source.isin(('yahoo_historical_chart', 'synthetic')).all():
        raise ValueError('Sector history requires an explicit supported source identity')
    data['date'] = pd.to_datetime(data.date, errors='raise')
    if (data.date.dt.tz is not None or data.date.isna().any()
            or not data.date.eq(data.date.dt.normalize()).all()
            or not data.date.dt.day_of_week.lt(5).all()):
        raise ValueError('Sector history requires naive US market dates')
    data['date'] = data.date.astype('datetime64[ns]')
    if data.duplicated(['date', 'symbol']).any():
        raise ValueError('Duplicate sector symbol/date')
    data['adj_close'] = pd.to_numeric(data.adj_close, errors='raise')
    if not (np.isfinite(data.adj_close).all() and data.adj_close.gt(0).all()):
        raise ValueError('Sector adjusted prices must be finite and positive')
    calendar = pd.DatetimeIndex(data.date.unique()).sort_values()
    rows = []
    for symbol in SECTOR_SYMBOLS:
        stock = data.loc[data.symbol.eq(symbol)].sort_values('date')
        prices = stock.set_index('date').adj_close.reindex(calendar)
        returns = prices.pct_change(fill_method=None)
        if np.isinf(returns).any():
            raise ValueError('Sector price ratios must be finite')
        for row in stock.itertuples(index=False):
            receipt = (row.date + pd.Timedelta(hours=17)).tz_localize('America/New_York').tz_convert('UTC')
            values = {'close': row.adj_close, 'return1': returns.loc[row.date]}
            for suffix, value in values.items():
                if pd.isna(value):
                    continue
                rows.append(dict(ticker='*', feature=f'us_sector_{symbol.lower()}_{suffix}', value=float(value),
                    published_at=receipt, available_at=receipt, observation_date=row.date,
                    source=row.source, original_pit_verified=False,
                    availability_basis='reconstructed_us_1700_no_original_receipt'))
    return pd.DataFrame(rows).sort_values(['available_at', 'feature']).reset_index(drop=True)
