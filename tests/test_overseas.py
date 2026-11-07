import numpy as np
import pandas as pd
import pytest


def overseas_events():
    local = ['2024-01-04 06:00', '2024-01-05 06:00', '2024-01-06 06:00',
             '2024-01-09 06:00', '2024-01-10 06:00', '2024-01-11 09:00']
    timestamps = pd.DatetimeIndex(local).tz_localize('Asia/Taipei').tz_convert('UTC')
    return pd.DataFrame(dict(ticker='*', feature='fmp_spy_close', value=[100., 105., 110., 115., 126., 999.],
                             published_at=timestamps, available_at=timestamps))


def overseas_frame():
    anchors = pd.to_datetime(['2024-01-03', '2024-01-04', '2024-01-05', '2024-01-10', '2024-01-10', '2024-01-11'])
    decisions = pd.DatetimeIndex(['2024-01-04 08:30', '2024-01-05 08:30', '2024-01-10 08:30',
                                 '2024-01-11 08:30', '2024-01-10 20:00', pd.NaT])
    return pd.DataFrame(dict(date=anchors, ticker='2330', decision_ts=decisions.tz_localize('Asia/Taipei').tz_convert('UTC'),
                             f_return_1=.01, target_1=.02), index=[8, 3, 6, 1, 5, 9])


def test_overseas_window_distinguishes_cumulative_new_information_stale_quotes_and_unknown_history():
    from twquant.overseas import add_overseas_window_features
    frame = overseas_frame()
    out = add_overseas_window_features(frame, overseas_events())
    pd.testing.assert_frame_equal(out[frame.columns], frame)
    np.testing.assert_allclose(out.f_ext_fmp_spy_window_return, [np.nan, .05, .2, 0., 0., np.nan], equal_nan=True)
    np.testing.assert_allclose(out.f_ext_fmp_spy_window_updated, [np.nan, 1., 1., 0., 0., np.nan], equal_nan=True)
    assert 'f_ext_fmp_spy_close' not in out


def test_overseas_window_is_causal_scale_free_and_broadcast_without_using_external_stock_prices():
    from twquant.overseas import add_overseas_window_features
    frame = overseas_frame().iloc[[1]].copy()
    events = overseas_events()
    # A receipt at the anchor is already in the base; a receipt at the
    # decision is visible. Anything after the decision stays unavailable.
    times = pd.DatetimeIndex(['2024-01-04 20:00', '2024-01-05 08:30', '2024-01-05 08:30:00.000001']).tz_localize('Asia/Taipei').tz_convert('UTC')
    boundary = pd.DataFrame(dict(ticker='*', feature='fmp_spy_close', value=[102., 107., 5000.],
                                 published_at=times, available_at=times))
    events = pd.concat([events, boundary], ignore_index=True)
    a = add_overseas_window_features(frame, events)
    assert a.f_ext_fmp_spy_window_return.iloc[0] == pytest.approx(107 / 102 - 1)
    changed = events.copy()
    changed.loc[changed.available_at.gt(frame.decision_ts.iloc[0]), 'value'] *= 100
    pd.testing.assert_frame_equal(a, add_overseas_window_features(frame, changed))
    scaled = events.assign(value=events.value * 1000)
    pd.testing.assert_frame_equal(a, add_overseas_window_features(frame, scaled))
    external = frame.assign(ticker='1216', f_return_1=900., target_1=-.9)
    b = add_overseas_window_features(external, events)
    for c in ['f_ext_fmp_spy_window_return', 'f_ext_fmp_spy_window_updated']:
        pd.testing.assert_series_equal(a[c], b[c])


@pytest.mark.parametrize('problem', ['absent', 'nonpositive', 'specific', 'early', 'naive'])
def test_overseas_window_rejects_invalid_inputs(problem):
    from twquant.overseas import add_overseas_window_features
    frame, events = overseas_frame(), overseas_events()
    if problem == 'absent':
        events = events.assign(feature='fred_unrate')
    elif problem == 'nonpositive':
        events.loc[0, 'value'] = 0
    elif problem == 'specific':
        events.loc[0, 'ticker'] = '1216'
    elif problem == 'early':
        frame.loc[3, 'decision_ts'] = pd.Timestamp('2024-01-04T10:00Z')
    else:
        events['available_at'] = events.available_at.dt.tz_localize(None)
    with pytest.raises(ValueError):
        add_overseas_window_features(frame, events)
