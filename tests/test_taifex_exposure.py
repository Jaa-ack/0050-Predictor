import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def exposure_inputs():
    dates = pd.bdate_range('2024-01-02', periods=100)
    factor = np.random.default_rng(37).normal(0., .005, len(dates))
    factor[0] = np.nan
    closes = 100 * np.cumprod(1 + np.nan_to_num(factor))
    history = pd.DataFrame(dict(trade_date=dates, contract='TX', expiry='202412', session='regular',
                                open=closes, high=closes+1, low=closes-1, close=closes, volume=1000.))
    frames = []
    for ticker in (*TRAINING_TICKERS, '0050', '1216'):
        beta = -1. if ticker == '1216' else 2.
        f = pd.DataFrame(dict(date=dates, ticker=ticker, f_return_1=beta*factor,
                             f_ext_taifex_tx_night_return=.01, target_1=99.))
        f['decision_ts'] = (dates.tz_localize('Asia/Taipei') + pd.Timedelta(days=1,hours=8,minutes=30)).tz_convert('UTC')
        f.loc[len(f)-1,'decision_ts'] = pd.NaT
        frames.append(f)
    frame = pd.concat(frames,ignore_index=True).sample(frac=1,random_state=8)
    return frame,history


def test_rolling_sector_exposure_learns_past_response_and_preserves_unknowns_and_original_rows():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    out = add_taifex_exposure_features(frame,history)
    pd.testing.assert_frame_equal(out[frame.columns],frame)
    dates = sorted(frame.date.unique())
    early = out.date.lt(dates[30])
    assert out.loc[early,'f_ext_taifex_tx_beta60'].isna().all()
    known = out.date.ge(dates[30]) & out.decision_ts.notna()
    np.testing.assert_allclose(out.loc[known & out.ticker.eq('2330'),'f_ext_taifex_tx_beta60'],2.,atol=1e-10)
    np.testing.assert_allclose(out.loc[known & out.ticker.eq('1216'),'f_ext_taifex_tx_beta60'],-1.,atol=1e-10)
    np.testing.assert_allclose(out.loc[known & out.ticker.eq('2330'),'f_ext_taifex_tx_beta_night_return'],.02,atol=1e-10)
    assert out.loc[out.decision_ts.isna(),[c for c in out if c not in frame]].isna().all().all()
    assert out.f_ext_taifex_tf_beta60.isna().all()


def test_exposure_is_causal_ignores_targets_and_never_uses_external_assets_for_core_statistics():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    a = add_taifex_exposure_features(frame,history)
    cut = sorted(frame.date.unique())[65]
    changed = frame.copy()
    changed['target_1'] = -900.
    changed.loc[changed.date.gt(cut),'f_return_1'] = 100.
    changed.loc[changed.ticker.eq('1216'),'f_return_1'] = -100.
    future = history.copy()
    future.loc[future.trade_date.gt(cut),['open','high','low','close']] *= 100
    b = add_taifex_exposure_features(changed,future)
    columns = [c for c in a if c not in frame]
    mask = frame.date.le(cut) & frame.ticker.isin(TRAINING_TICKERS)
    pd.testing.assert_frame_equal(a.loc[mask,columns],b.loc[mask,columns])


def test_exposure_caps_extreme_response_and_keeps_zero_variance_or_missing_night_unknown():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    frame['f_return_1'] *= 100
    frame['f_ext_taifex_tx_night_return'] = np.nan
    a = add_taifex_exposure_features(frame,history)
    assert a.f_ext_taifex_tx_beta60.dropna().abs().eq(3.).all()
    assert a.f_ext_taifex_tx_beta_night_return.isna().all()
    flat = history.assign(open=100.,high=101.,low=99.,close=100.)
    b = add_taifex_exposure_features(frame,flat)
    assert b.f_ext_taifex_tx_beta60.isna().all()


def test_exposure_rejects_decisions_before_the_regular_session_receipt_contract():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    frame['decision_ts'] = (pd.to_datetime(frame.date).dt.tz_localize('Asia/Taipei')+pd.Timedelta(hours=19)).dt.tz_convert('UTC')
    with pytest.raises(ValueError): add_taifex_exposure_features(frame,history)


def test_contract_roll_and_missing_sessions_do_not_fabricate_daily_returns():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    near = history.assign(expiry='202402')
    far = history.assign(expiry='202406')
    far[['open','high','low','close']] *= 10
    combined = pd.concat([near,far],ignore_index=True)
    # A missing report makes both that session and the following return unknown.
    missing = sorted(frame.date.unique())[15]
    combined = combined.loc[combined.trade_date.ne(missing)]
    a = add_taifex_exposure_features(frame,combined)
    np.testing.assert_allclose(a.loc[a.ticker.eq('2330'),'f_ext_taifex_tx_beta60'].dropna(),2.,atol=1e-10)
    assert a.loc[a.date.eq(sorted(frame.date.unique())[31]),'f_ext_taifex_tx_beta60'].isna().all()


def test_pairwise_missing_stock_returns_and_external_calendar_do_not_change_core_exposures():
    from twquant.taifex_exposure import add_taifex_exposure_features
    frame,history = exposure_inputs()
    frame.loc[frame.ticker.eq('2330') & frame.date.isin(sorted(frame.date.unique())[20:35]),'f_return_1'] = np.nan
    a = add_taifex_exposure_features(frame,history)
    np.testing.assert_allclose(a.loc[a.ticker.eq('2330'),'f_ext_taifex_tx_beta60'].dropna(),2.,atol=1e-10)
    extra = frame.loc[frame.ticker.eq('1216')].iloc[[0]].copy()
    extra['date'] = pd.Timestamp('2024-01-06')  # Outside the trusted session calendar.
    extra['decision_ts'] = pd.Timestamp('2024-01-07T00:30:00Z')
    b = add_taifex_exposure_features(pd.concat([frame,extra],ignore_index=True),history)
    new = [c for c in a if c not in frame]
    pd.testing.assert_frame_equal(a[new].reset_index(drop=True),b.iloc[:-1][new])
    assert b.iloc[-1][new].isna().all()
