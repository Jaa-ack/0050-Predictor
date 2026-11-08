import numpy as np
import pandas as pd
import pytest


SYMBOLS = ('XLF','XLP','XLC','XLI','XLB','XLE','SMH')


def sector_history():
    dates = pd.to_datetime(['2024-01-02','2024-01-03','2024-01-04','2024-07-01','2024-07-02'])
    frames = [pd.DataFrame(dict(date=dates,symbol=s,adj_close=[100.,105.,110.,120.,126.],
                               source='yahoo_historical_chart')) for s in SYMBOLS]
    return pd.concat(frames,ignore_index=True).sample(frac=1,random_state=7)


def test_sector_events_preserve_source_and_reconstruct_local_receipts_across_dst():
    from twquant.us_sectors import sector_price_events
    from twquant.features import point_in_time_join
    events = sector_price_events(sector_history())
    assert events.source.eq('yahoo_historical_chart').all()
    assert events.ticker.eq('*').all() and not events.original_pit_verified.any()
    assert events.availability_basis.eq('reconstructed_us_1700_no_original_receipt').all()
    close = events.loc[events.feature.eq('us_sector_xlf_close')].sort_values('observation_date')
    assert close.available_at.iloc[0] == pd.Timestamp('2024-01-02T22:00:00Z')
    assert close.available_at.iloc[-1] == pd.Timestamp('2024-07-02T21:00:00Z')
    ret = events.loc[events.feature.eq('us_sector_xlf_return1')].sort_values('observation_date')
    np.testing.assert_allclose(ret.value,[.05,110/105-1,120/110-1,.05])
    decisions = pd.DataFrame(dict(ticker='2330',decision_ts=pd.to_datetime([
        '2024-01-02T21:59:59Z','2024-01-02T22:00:00Z','2024-01-03T22:00:00Z'],utc=True)))
    out = point_in_time_join(decisions,events)
    assert pd.isna(out.f_ext_us_sector_xlf_close.iloc[0])
    assert out.f_ext_us_sector_xlf_close.iloc[1] == 100.
    assert out.f_ext_us_sector_xlf_return1.iloc[2] == pytest.approx(.05)


def test_sector_events_are_causal_scale_free_and_keep_missing_series_history_unknown():
    from twquant.us_sectors import sector_price_events
    frame = sector_history()
    frame = frame.loc[~(frame.symbol.eq('XLC') & frame.date.lt('2024-01-04'))]
    frame = frame.loc[~(frame.symbol.eq('XLF') & frame.date.eq('2024-01-03'))]
    a = sector_price_events(frame)
    assert not (a.feature.str.contains('xlc') & a.observation_date.lt('2024-01-04')).any()
    assert not (a.feature.eq('us_sector_xlf_return1') & a.observation_date.eq('2024-01-04')).any()
    changed = frame.copy()
    changed['target_1'] = -500.
    changed.loc[changed.date.ge('2024-07-01'),'adj_close'] *= 100
    b = sector_price_events(changed)
    pd.testing.assert_frame_equal(a.loc[a.observation_date.lt('2024-07-01')],b.loc[b.observation_date.lt('2024-07-01')])
    scaled = sector_price_events(frame.assign(adj_close=frame.adj_close*1000))
    mask = a.feature.str.endswith('_return1')
    np.testing.assert_allclose(a.loc[mask,'value'],scaled.loc[mask,'value'])
    synthetic = sector_price_events(frame.assign(source='synthetic'))
    assert synthetic.source.eq('synthetic').all()


@pytest.mark.parametrize('problem',['missing_symbol','unknown_symbol','duplicate','nonpositive','infinite','timestamp','source'])
def test_sector_events_reject_ambiguous_or_invalid_history(problem):
    from twquant.us_sectors import sector_price_events
    frame = sector_history()
    if problem == 'missing_symbol': frame = frame.loc[frame.symbol.ne('XLF')]
    elif problem == 'unknown_symbol': frame.loc[frame.symbol.eq('XLF'),'symbol'] = '2330'
    elif problem == 'duplicate': frame = pd.concat([frame,frame.iloc[[0]]])
    elif problem == 'nonpositive': frame.iloc[0,frame.columns.get_loc('adj_close')] = 0.
    elif problem == 'infinite': frame.iloc[0,frame.columns.get_loc('adj_close')] = np.inf
    elif problem == 'timestamp': frame['date'] += pd.Timedelta(hours=1)
    else: frame['source'] = 'unverified_vendor'
    with pytest.raises(ValueError): sector_price_events(frame)


def test_sector_window_is_isolated_from_default_fmp_and_rejects_company_specific_inputs():
    from test_overseas import overseas_events,overseas_frame
    from twquant.overseas import add_overseas_window_features
    frame,original = overseas_frame(),overseas_events()
    sectors = original.assign(feature='us_sector_xlf_close')
    combined = pd.concat([original,sectors],ignore_index=True)
    baseline = add_overseas_window_features(frame,original)
    pd.testing.assert_frame_equal(baseline,add_overseas_window_features(frame,combined))
    out = add_overseas_window_features(baseline,combined,event_prefix='us_sector')
    pd.testing.assert_frame_equal(out[baseline.columns],baseline)
    np.testing.assert_allclose(out.f_ext_us_sector_xlf_window_return,baseline.f_ext_fmp_spy_window_return,equal_nan=True)
    with pytest.raises(ValueError):
        add_overseas_window_features(frame,sectors.assign(ticker='1216'),event_prefix='us_sector')
    with pytest.raises(ValueError):
        add_overseas_window_features(frame,sectors,event_prefix='us_sector|fmp')


def test_sector_profile_adds_relative_context_without_changing_original_profiles():
    from twquant.features import select_feature_columns
    columns = ['f_return_1','f_path_up_1_60','f_ext_fmp_spy_return1','f_ext_taifex_tx_night_return',
        'f_ext_fred_vix','f_ext_us_sector_xlf_return1','f_ext_us_sector_xlf_return1_age_days',
        'f_ext_us_sector_xlf_window_return','f_ext_us_sector_xlf_window_updated',
        'f_ext_us_sector_xlf_close','f_ext_us_sector_xlf_close_age_days']
    frame = pd.DataFrame(columns=columns)
    base = frame[columns[:5]]
    for profile in ('portable_price','portable_market','portable_all','portable_path'):
        assert select_feature_columns(frame,profile)==select_feature_columns(base,profile)
    assert select_feature_columns(frame,'portable_sector')==columns[:4]+columns[5:9]
