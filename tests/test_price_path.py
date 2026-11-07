import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def path_inputs():
    dates = pd.bdate_range('2022-01-03', periods=320)
    returns = np.tile([.01, 0., -.02, 0.], 80)
    frames = []
    for ticker in (*TRAINING_TICKERS, '0050', '1216'):
        frame = pd.DataFrame(dict(date=dates, ticker=ticker, f_return_1=returns,
            f_return_5=returns*2, f_return_20=returns*3, target_1=10.))
        frame['decision_ts'] = (dates.tz_localize('Asia/Taipei') + pd.Timedelta(days=1,hours=8,minutes=30)).tz_convert('UTC')
        frame.loc[len(frame)-1, 'decision_ts'] = pd.NaT
        frames.append(frame)
    return pd.concat(frames,ignore_index=True).sample(frac=1,random_state=7)


def test_path_statistics_distinguish_up_flat_and_persistence_with_known_minimum_history():
    from twquant.price_path import add_price_path_features
    frame = path_inputs()
    out = add_price_path_features(frame)
    pd.testing.assert_frame_equal(out[frame.columns],frame)
    dates = sorted(frame.date.unique())
    for h in (1,5,20):
        for window, first in [(60,29),(252,125)]:
            assert out.loc[out.date.lt(dates[first]),f'f_path_up_{h}_{window}'].isna().all()
            at = out.date.eq(dates[251])
            np.testing.assert_allclose(out.loc[at,f'f_path_up_{h}_{window}'],.25)
            np.testing.assert_allclose(out.loc[at,f'f_path_flat_{h}_{window}'],.5)
    assert out.loc[out.date.lt(dates[30]),'f_path_same_sign_1_60'].isna().all()
    assert out.f_path_same_sign_1_60.dropna().eq(0).all()
    new = [c for c in out if c not in frame]
    assert len(new) == 13
    assert out.loc[out.decision_ts.isna(),new].isna().all().all()


def test_path_features_ignore_targets_future_returns_and_other_assets():
    from twquant.price_path import add_price_path_features
    frame = path_inputs()
    cut = sorted(frame.date.unique())[200]
    original = add_price_path_features(frame)
    changed = frame.copy()
    changed['target_1'] = -500.
    columns = ['f_return_1','f_return_5','f_return_20']
    changed.loc[changed.date.gt(cut),columns] = 100.
    changed.loc[changed.ticker.eq('1216'),columns] = -100.
    out = add_price_path_features(changed)
    mask = frame.date.le(cut) & frame.ticker.isin(TRAINING_TICKERS)
    new = [c for c in out if c not in frame]
    pd.testing.assert_frame_equal(original.loc[mask,new],out.loc[mask,new])


def test_missing_returns_are_unknown_instead_of_flat_or_down_and_use_valid_counts():
    from twquant.price_path import add_price_path_features
    frame = path_inputs()
    dates = sorted(frame.date.unique())
    frame.loc[frame.date.lt(dates[25]),'f_return_1'] = np.nan
    frame.loc[frame.date.eq(dates[25]),'f_return_1'] = np.inf
    frame['f_return_5'] = np.nan
    frame['f_return_20'] = 0.
    out = add_price_path_features(frame)
    assert out.filter(regex='f_path_.*_5_').isna().all().all()
    assert out.loc[out.date.lt(dates[55]),'f_path_up_1_60'].isna().all()
    assert out.f_path_up_20_60.dropna().eq(0.).all()
    assert out.f_path_flat_20_60.dropna().eq(1.).all()
    at = out.date.eq(dates[59])
    valid = frame.loc[frame.ticker.eq('2330') & frame.date.le(dates[59]),'f_return_1']
    valid = valid[np.isfinite(valid)]
    np.testing.assert_allclose(out.loc[at,'f_path_up_1_60'],valid.gt(0).mean())
    np.testing.assert_allclose(out.loc[at,'f_path_flat_1_60'],valid.eq(0).mean())


def test_external_only_dates_do_not_extend_windows_or_receive_path_features():
    from twquant.price_path import add_price_path_features
    frame = path_inputs()
    original = add_price_path_features(frame)
    extra = frame.loc[frame.ticker.eq('1216')].iloc[[0]].copy()
    extra['date'] = pd.Timestamp('2022-01-08')
    extra['decision_ts'] = pd.Timestamp('2022-01-09T00:30:00Z')
    out = add_price_path_features(pd.concat([frame,extra],ignore_index=True))
    new = [c for c in out if c not in frame]
    pd.testing.assert_frame_equal(original[new].reset_index(drop=True),out.iloc[:-1][new])
    assert out.iloc[-1][new].isna().all()


def test_path_features_require_known_price_anchor_receipt_and_unique_sessions():
    from twquant.price_path import add_price_path_features
    frame = path_inputs()
    frame['decision_ts'] = (pd.to_datetime(frame.date).dt.tz_localize('Asia/Taipei')+pd.Timedelta(hours=20)).dt.tz_convert('UTC')
    assert add_price_path_features(frame).f_path_up_1_60.notna().any()
    with pytest.raises(ValueError):
        add_price_path_features(pd.concat([frame,frame.iloc[[0]]]))
    early = frame.copy()
    early['decision_ts'] -= pd.Timedelta(seconds=1)
    with pytest.raises(ValueError): add_price_path_features(early)
    naive = frame.copy()
    naive['decision_ts'] = naive.decision_ts.dt.tz_localize(None)
    with pytest.raises(ValueError): add_price_path_features(naive)


def test_path_inputs_enter_only_the_explicit_path_profile():
    from twquant.features import select_feature_columns
    frame = pd.DataFrame(columns=['f_return_1','f_path_up_1_60','f_ext_fmp_spy_return1',
        'f_ext_taifex_tx_night_return','f_ext_fred_vix','f_ext_fmp_spy_close'])
    assert select_feature_columns(frame,'portable_price') == ['f_return_1']
    assert select_feature_columns(frame,'portable_market') == ['f_return_1','f_ext_fmp_spy_return1','f_ext_taifex_tx_night_return']
    assert select_feature_columns(frame,'portable_all') == ['f_return_1','f_ext_fmp_spy_return1','f_ext_taifex_tx_night_return','f_ext_fred_vix']
    assert select_feature_columns(frame,'portable_path') == list(frame.columns[:4])
