import numpy as np
import pandas as pd
import pytest


def proxy_frame():
    from test_direction import preopen_frame, search_frame
    frame = preopen_frame(search_frame())
    local = frame.decision_ts.dt.tz_convert('Asia/Taipei')
    frame['next_open_date'] = local.dt.tz_localize(None).dt.normalize()
    frame['close'] = 100.
    frame['next_open'] = 102.
    frame['f_volatility_20'] = .02
    return frame


def test_proxy_is_an_explicit_synthetic_raw_open_assumption_preserving_original_columns():
    from twquant.opening_proxy import add_opening_reference_proxy
    frame = proxy_frame()
    out = add_opening_reference_proxy(frame)
    pd.testing.assert_frame_equal(out[frame.columns], frame)
    known = frame.decision_ts.notna()
    np.testing.assert_allclose(out.loc[known, 'f_openref_log_gap'], np.log(1.02))
    np.testing.assert_allclose(out.loc[known, 'f_openref_scaled_gap'], np.log(1.02)/.02)
    assert out.loc[known, 'f_openref_present'].eq(1).all()
    assert out.loc[~known, 'f_openref_log_gap'].isna().all()
    contract = out.attrs['opening_proxy_contract']
    assert contract['evidence_kind'] == 'synthetic'
    assert contract['uses_future_open'] is True
    assert contract['uses_future_closing_labels'] is False
    assert contract['original_pit_verified'] is False


def test_keyed_noise_and_missingness_ignore_row_order_future_labels_and_other_assets():
    from twquant.opening_proxy import add_opening_reference_proxy
    frame = proxy_frame()
    kwargs = dict(noise_bps=50., missing_fraction=.2, seed=17)
    out = add_opening_reference_proxy(frame, **kwargs)
    changed = frame.sample(frac=1, random_state=4).copy()
    changed[['target_1', 'target_5', 'target_20']] = 900.
    changed['next_open_adjustment'] = 100.
    changed['next_high'] = 99999.
    changed['next_volume'] = 0.
    other = ~changed.ticker.isin(__import__('twquant.universe', fromlist=['TRAINING_TICKERS']).TRAINING_TICKERS)
    changed.loc[other, 'next_open'] *= 2
    actual = add_opening_reference_proxy(changed, **kwargs)
    columns = [c for c in out if c.startswith('f_openref_')]
    core = ~other
    pd.testing.assert_frame_equal(out.loc[changed.loc[core].index, columns], actual.loc[core, columns])
    prefix = frame.iloc[:100].copy()
    pd.testing.assert_frame_equal(add_opening_reference_proxy(prefix, **kwargs)[columns], out.iloc[:100][columns])
    different = add_opening_reference_proxy(frame, **{**kwargs, 'seed': 29})
    assert not out[columns].equals(different[columns])


def test_missing_reference_is_unknown_and_does_not_remove_rows_or_abstain():
    from twquant.opening_proxy import add_opening_reference_proxy
    frame = proxy_frame()
    frame.loc[frame.index[:2], 'next_open'] = [np.nan, 0.]
    out = add_opening_reference_proxy(frame)
    assert len(out) == len(frame)
    assert out.iloc[:2].f_openref_log_gap.isna().all()
    assert out.iloc[:2].f_openref_present.eq(0).all()
    absent = add_opening_reference_proxy(frame, missing_fraction=1.)
    assert absent.f_openref_log_gap.isna().all()
    assert absent.f_openref_present.eq(0).all()


@pytest.mark.parametrize('problem', ['negative_noise', 'nonfinite_noise', 'missingness', 'seed', 'duplicate', 'naive', 'postclose', 'wrong_session'])
def test_proxy_rejects_invalid_settings_and_clock_contract(problem):
    from twquant.opening_proxy import add_opening_reference_proxy
    frame = proxy_frame(); kwargs = {}
    if problem == 'negative_noise': kwargs['noise_bps'] = -1
    elif problem == 'nonfinite_noise': kwargs['noise_bps'] = np.inf
    elif problem == 'missingness': kwargs['missing_fraction'] = 1.1
    elif problem == 'seed': kwargs['seed'] = True
    elif problem == 'duplicate': frame = pd.concat([frame, frame.iloc[[0]]])
    elif problem == 'naive': frame['decision_ts'] = frame.decision_ts.dt.tz_localize(None)
    elif problem == 'postclose': frame['decision_ts'] += pd.Timedelta(hours=11, minutes=30)
    else: frame['next_open_date'] -= pd.Timedelta(days=1)
    with pytest.raises(ValueError): add_opening_reference_proxy(frame, **kwargs)


def test_opening_profile_is_isolated_from_every_existing_portable_profile():
    from twquant.features import select_feature_columns
    frame = pd.DataFrame(columns=['f_return_1', 'f_path_up_1_60', 'f_flow_foreign_imbalance',
                                 'f_openref_log_gap', 'f_ext_us_sector_xlf_return1'])
    base = frame.drop(columns='f_openref_log_gap')
    for profile in ('portable_price', 'portable_market', 'portable_all', 'portable_path', 'portable_sector', 'portable_flow'):
        assert select_feature_columns(frame, profile) == select_feature_columns(base, profile)
    assert select_feature_columns(frame, 'portable_opening') == list(frame)


def test_direction_research_requires_opening_proxy_contract():
    from test_direction import preopen_frame, search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    frame = preopen_frame(search_frame()).assign(f_openref_log_gap=.01)
    with pytest.raises(ValueError, match='opening.*contract'):
        search_direction_models(frame, small_config(horizons=(1,), max_folds=1),
            candidates=(DirectionCandidate('prior'),), holdout_dates=15, bootstrap_samples=10, mode='preopen')


def test_engine_forces_synthetic_evidence_complete_predictions_and_versions_the_assumption():
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    from twquant.opening_proxy import add_opening_reference_proxy
    cfg = small_config(horizons=(1,), max_folds=1, evaluation_tickers=('0050', '1216'))
    candidates = (DirectionCandidate('prior'),)
    a = search_direction_models(add_opening_reference_proxy(proxy_frame()), cfg,
        candidates=candidates, holdout_dates=15, bootstrap_samples=10, mode='preopen', evidence_kind='historical')
    b = search_direction_models(add_opening_reference_proxy(proxy_frame(), missing_fraction=1.), cfg,
        candidates=candidates, holdout_dates=15, bootstrap_samples=10, mode='preopen', evidence_kind='historical')
    assert a.report['by_horizon']['1']['evidence_kind'] == 'synthetic'
    assert b.report['by_horizon']['1']['coverage'] == 1.
    assert a.report['protocol']['opening_proxy_contract']['uses_future_open'] is True
    np.testing.assert_array_equal(a.predictions.probability_up, b.predictions.probability_up)
    assert set(a.predictions.model_version).isdisjoint(b.predictions.model_version)
    assert a.report['real_world_target_verified'] is False
