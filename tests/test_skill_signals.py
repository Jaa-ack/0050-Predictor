import numpy as np
import pandas as pd
import pytest

from test_features import prices_fixture
from twquant.features import build_features, observed_market_sessions


def signals(prices, mode='postclose'):
    from twquant.skill_signals import add_daily_signals
    return add_daily_signals(build_features(prices, mode=mode), observed_market_sessions(prices))


def test_breakout_excludes_current_close_and_uses_adjusted_prices():
    from twquant.skill_signals import SIGNAL_COLUMNS
    prices = prices_fixture(45)
    result = signals(prices)
    stock = result.loc[result.ticker.eq('2330')].reset_index(drop=True)
    assert stock.loc[:19, 'f_scan_breakout_20d'].isna().all()
    assert stock.loc[20:, 'f_scan_breakout_20d'].eq(1.).all()
    assert set(SIGNAL_COLUMNS) == {c for c in result if c.startswith('f_scan_')}
    split = prices.copy()
    split.loc[split.date.ge(prices.date.unique()[25]), ['open', 'high', 'low', 'close']] /= 4
    pd.testing.assert_frame_equal(result[list(SIGNAL_COLUMNS)], signals(split)[list(SIGNAL_COLUMNS)])


def test_five_daily_formulas_and_reused_rsi_have_explicit_boundaries():
    from twquant.skill_signals import add_daily_signals
    dates = pd.bdate_range('2024-01-01', periods=31)
    closes = [100.] * 10 + list(np.linspace(100, 112, 11)) + [113.] * 10
    frame = pd.DataFrame(dict(ticker='2330', date=dates, adj_close=closes, f_rsi_14=.5,
        decision_ts=(dates + pd.Timedelta(hours=20)).tz_localize('Asia/Taipei'),
        available_at=(dates + pd.Timedelta(hours=18)).tz_localize('Asia/Taipei')))
    frame.loc[29, 'f_rsi_14'] = .29
    frame.loc[30, 'adj_close'] = 113.5
    frame.loc[30, 'f_rsi_14'] = .30
    result = add_daily_signals(frame, dates)
    assert result.loc[25, 'f_scan_bull_flag'] == 1
    assert result.loc[29, 'f_scan_oversold'] == 1
    assert result.loc[30, 'f_scan_oversold'] == 0
    assert result.loc[30, 'f_scan_oversold_bounce'] == 1
    frame.loc[29, 'adj_close'] = 90
    frame.loc[30, 'adj_close'] = 115
    assert add_daily_signals(frame, dates).loc[30, 'f_scan_reclaim_ma20'] == 1


@pytest.mark.parametrize('mode', ['postclose', 'preopen'])
def test_future_prices_and_transfer_assets_never_change_earlier_signals(mode):
    from twquant.skill_signals import SIGNAL_COLUMNS
    prices = prices_fixture(65)
    before = signals(prices, mode)
    changed = prices.copy()
    cutoff = sorted(prices.date.unique())[45]
    changed.loc[changed.date.ge(cutoff), ['open', 'high', 'low', 'close', 'adj_close']] *= 2
    after = signals(changed, mode)
    mask = before.date.lt(cutoff)
    pd.testing.assert_frame_equal(before.loc[mask, list(SIGNAL_COLUMNS)], after.loc[mask, list(SIGNAL_COLUMNS)])
    external = prices.loc[prices.ticker.eq('0050')].copy()
    external['ticker'] = '1216'
    external[['open', 'high', 'low', 'close', 'adj_close']] *= 3
    expanded = signals(pd.concat([prices, external], ignore_index=True), mode)
    pd.testing.assert_frame_equal(before, expanded.loc[expanded.ticker.ne('1216')].reset_index(drop=True))


def test_missing_and_late_sessions_are_not_compressed_into_shorter_windows():
    prices = prices_fixture(60)
    dates = sorted(prices.date.unique())
    missing = prices.loc[~(prices.ticker.eq('2330') & prices.date.eq(dates[25]))]
    stock = signals(missing).loc[lambda x: x.ticker.eq('2330')].set_index('date')
    assert stock.loc[dates[26:46], 'f_scan_breakout_20d'].isna().all()
    assert stock.loc[dates[46], 'f_scan_breakout_20d'] == 1
    prices.loc[prices.ticker.eq('2330') & prices.date.eq(dates[25]), 'available_at'] += pd.Timedelta(days=4)
    late = signals(prices).loc[lambda x: x.ticker.eq('2330')].set_index('date')
    pd.testing.assert_series_equal(stock.f_scan_breakout_20d, late.f_scan_breakout_20d)


def test_flat_zero_volume_empty_duplicate_unsorted_and_invalid_calendar():
    from twquant.skill_signals import add_daily_signals, SIGNAL_COLUMNS
    prices = prices_fixture(45)
    prices[['open', 'high', 'low', 'close', 'adj_close']] = 100.
    prices['volume'] = 0.
    frame = build_features(prices)
    result = add_daily_signals(frame, observed_market_sessions(prices))
    assert np.isfinite(result.loc[result.date.eq(prices.date.max()), list(SIGNAL_COLUMNS)]).all().all()
    assert result[list(SIGNAL_COLUMNS)].fillna(0).eq(0).all().all()
    assert add_daily_signals(frame.iloc[:0], observed_market_sessions(prices)).empty
    for bad in [pd.concat([frame, frame.iloc[[0]]]), frame.iloc[::-1]]:
        with pytest.raises(ValueError, match='duplicate|sort'):
            add_daily_signals(bad, observed_market_sessions(prices))
    with pytest.raises(ValueError, match='calendar'):
        add_daily_signals(frame, observed_market_sessions(prices)[::-1])


def test_extreme_finite_prices_produce_unknown_arithmetic_instead_of_infinite_flags():
    from twquant.skill_signals import add_daily_signals
    frame = build_features(prices_fixture(35))
    stock = frame.loc[frame.ticker.eq('2330')].copy()
    stock['adj_close'] = 1e-300
    stock.loc[stock.index[-10:], 'adj_close'] = 1e300
    result = add_daily_signals(stock, pd.DatetimeIndex(stock.date))
    assert result.iloc[-1].f_scan_bull_flag != 1
    assert pd.isna(result.iloc[-1].f_scan_bull_flag)
