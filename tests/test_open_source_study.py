import json

import numpy as np
import pandas as pd
import pytest

from test_fixed_universe import expanded_prices
from test_research import small_config
from twquant.universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS


def study_config():
    return small_config(horizons=(1, 5), max_folds=1, feature_profile='portable_price',
                        evaluation_tickers=DEFAULT_EVALUATION_TICKERS)


def test_study_preserves_matched_folds_simple_baselines_and_frozen_interval_predictions(tmp_path):
    from twquant.open_source_study import run_study
    from twquant.features import build_features, select_feature_columns
    from twquant.comparison import reserve_development
    from twquant.research import run_research
    _, prices = expanded_prices(150)
    folder = tmp_path / 'study'
    cfg = study_config()
    report = run_study(prices, cfg, holdout_dates=15, output=folder)
    assert report['data_kind'] == 'synthetic' and report['holdout_opened'] is False
    assert report['training_tickers'] == list(TRAINING_TICKERS)
    assert report['causality']['passed']
    assert set(report['variants']) == {'current', 'alpha', 'pooled_exact', 'vol_scaled'}
    a = pd.read_parquet(folder / 'current/predictions.parquet')
    b = pd.read_parquet(folder / 'alpha/predictions.parquet')
    for name in ['alpha', 'pooled_exact', 'vol_scaled']:
        p = pd.read_parquet(folder / name / 'predictions.parquet')
        pd.testing.assert_frame_equal(a[['date', 'ticker', 'horizon', 'actual_return']],
                                      p[['date', 'ticker', 'horizon', 'actual_return']])
        if name != 'alpha':
            pd.testing.assert_frame_equal(a[['probability_up', 'expected_return']],
                                          p[['probability_up', 'expected_return']])
    dev, _ = reserve_development(build_features(prices), cfg.horizons, 15)
    existing = run_research(dev, select_feature_columns(dev, cfg.feature_profile), cfg).predictions
    np.testing.assert_allclose(a.probability_up, existing.probability_up)
    np.testing.assert_allclose(a.expected_return, existing.expected_return)
    for name in report['variants']:
        transfer = pd.read_parquet(folder / name / 'transfer_predictions.parquet')
        assert set(transfer.ticker) == set(DEFAULT_EVALUATION_TICKERS)
        assert set(transfer.model_version) <= set(pd.read_parquet(folder / name / 'predictions.parquet').model_version)
    assert report['simple_baselines']['1']['frequency']['brier'] >= 0
    assert report['variants']['current']['direction_audit']['1']['coverage'] == 1
    assert json.loads((folder / 'protocol.json').read_text())['holdout_opened'] is False
    assert (folder / 'artifact_hashes.json').is_file()
    assert not (tmp_path / 'current.json').exists()
    assert len(a) == len(b)
    with pytest.raises(FileExistsError):
        run_study(prices, cfg, holdout_dates=15, output=folder)


def test_reserved_and_external_changes_cannot_change_primary_predictions(tmp_path):
    from twquant.open_source_study import run_study
    _, prices = expanded_prices(145)
    cfg = study_config()
    a, b = tmp_path / 'a', tmp_path / 'b'
    run_study(prices, cfg, holdout_dates=15, output=a)
    changed = prices.copy()
    boundary = sorted(prices.date.unique())[-15]
    mask = changed.date.ge(boundary) | changed.ticker.isin(DEFAULT_EVALUATION_TICKERS[1:])
    changed.loc[mask, ['open', 'high', 'low', 'close', 'adj_close']] *= 3
    changed.loc[mask, 'volume'] *= 7
    run_study(changed, cfg, holdout_dates=15, output=b)
    for name in ['current', 'alpha', 'pooled_exact', 'vol_scaled']:
        pd.testing.assert_frame_equal(pd.read_parquet(a / name / 'predictions.parquet'),
                                      pd.read_parquet(b / name / 'predictions.parquet'))


def test_missing_decisions_and_answers_stay_in_full_calendar_denominator(tmp_path):
    from twquant.open_source_study import run_study
    _, prices = expanded_prices(150)
    cfg = study_config()
    # h1's final planned test window is indices 131..133, before the 15-day reserve.
    missing_day = sorted(prices.date.unique())[-18]
    prices = prices[~(prices.ticker.isin(TRAINING_TICKERS) & prices.date.eq(missing_day))]
    report = run_study(prices, cfg, holdout_dates=15, output=tmp_path / 'gaps')
    audit = report['variants']['current']['direction_audit']['1']
    assert audit['missing_predictions'] >= 10
    assert audit['unresolved_labels'] >= 10
    assert audit['coverage'] < 1


def test_study_rejects_other_targets_and_incomplete_settings(tmp_path):
    from dataclasses import replace
    from twquant.open_source_study import run_study
    _, prices = expanded_prices(150)
    with pytest.raises(ValueError, match='postclose close_return'):
        run_study(prices, replace(study_config(), objective='open_return'),
                  holdout_dates=15, output=tmp_path / 'invalid')


def test_cost_stress_keeps_marginal_selected_trades_and_base_weights(tmp_path):
    from twquant.open_source_study import _cost_stress
    from twquant.research import simulate_portfolio
    cfg = small_config(horizons=(1,), min_probability=.5, max_weight=.8)
    p = pd.DataFrame(dict(ticker=['2330'], date=[pd.Timestamp('2024-01-02')], horizon=[1],
        next_open=[100.], next_open_date=[pd.Timestamp('2024-01-03')],
        probability_up=[.6], expected_return=[.009], trade_return_1=[.01],
        benchmark_trade_return_1=[.01], benchmark_target_1=[.01]))
    result = _cost_stress(p, cfg, tmp_path)
    _, reference = simulate_portfolio(p, cfg)
    assert result['base']['strategy'] == reference['strategy']
    assert result['base']['orders'] == result['double_slippage']['orders'] == 2
    assert result['base']['gross_strategy'] == result['double_slippage']['gross_strategy']
    assert result['base']['strategy']['total_return'] > result['double_slippage']['strategy']['total_return']
