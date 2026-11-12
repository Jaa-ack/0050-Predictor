import json

import numpy as np
import pandas as pd
import pytest

from test_fixed_universe import expanded_prices
from test_research import small_config
from twquant.features import build_features, select_feature_columns
from twquant.comparison import reserve_development
from twquant.research import run_research
from twquant.universe import TRAINING_TICKERS, DEFAULT_EVALUATION_TICKERS


def test_matched_abcd_and_rule_study_preserves_baseline_and_all_predictions(tmp_path):
    from twquant.skill_integration import compare_skill_modules
    _, prices = expanded_prices(160)
    cfg = small_config(horizons=(1, 5, 20), max_folds=1, feature_profile='portable_price',
                       evaluation_tickers=DEFAULT_EVALUATION_TICKERS)
    output = tmp_path / 'study'
    report = compare_skill_modules(prices, cfg, holdout_dates=15, output=output)
    frame = build_features(prices)
    dev, _ = reserve_development(frame, cfg.horizons, 15)
    baseline = run_research(dev, select_feature_columns(frame, cfg.feature_profile), cfg)
    a = pd.read_parquet(output / 'A/predictions.parquet')
    b = pd.read_parquet(output / 'B/predictions.parquet')
    # Mixed-horizon nullable execution fields undergo the same Parquet roundtrip.
    baseline.predictions.to_parquet(tmp_path / 'expected.parquet', index=False)
    pd.testing.assert_frame_equal(a, pd.read_parquet(tmp_path / 'expected.parquet'))
    pd.testing.assert_frame_equal(a[['date', 'ticker', 'horizon', 'actual_return']],
                                  b[['date', 'ticker', 'horizon', 'actual_return']])
    assert report['holdout_opened'] is False and report['data_kind'] == 'synthetic'
    assert report['training_tickers'] == list(TRAINING_TICKERS)
    for name in ['C', 'D']:
        assert report['variants'][name]['prediction_source'] == 'A'
        assert report['variants'][name]['prediction_metrics'] == report['variants']['A']['prediction_metrics']
        assert report['variants'][name]['direction_audit'] == report['variants']['A']['direction_audit']
    assert report['variants']['R']['prediction_metrics'] is None
    for name in ['A', 'B']:
        transfer = pd.read_parquet(output / f'{name}/transfer_predictions.parquet')
        assert set(transfer.ticker) == set(DEFAULT_EVALUATION_TICKERS)
        assert set(transfer.model_version) <= set(pd.read_parquet(output / f'{name}/predictions.parquet').model_version)
    for horizon in ['5', '20']:
        paired = report['paired_brier_B_minus_A'][horizon]
        assert paired['requested_block_dates'] >= int(horizon)
        if paired['dates'] < paired['requested_block_dates']:
            assert paired['ci95'] is None and paired['status'] == 'insufficient_dates_for_block_uncertainty'
    for name in ['C', 'D', 'R']:
        decisions = pd.read_parquet(output / f'{name}/decisions.parquet')
        assert set(decisions.ticker) == set(TRAINING_TICKERS)
        assert len(decisions) == a.horizon.eq(1).sum()
    assert not (tmp_path / 'current.json').exists()
    assert json.loads((output / 'protocol.json').read_text())['rule_spec'] == report['rule_spec']
    with pytest.raises(FileExistsError):
        compare_skill_modules(prices, cfg, holdout_dates=15, output=output)


def test_holdout_and_external_changes_cannot_change_training_screening_or_choices(tmp_path):
    from twquant.skill_integration import compare_skill_modules
    _, prices = expanded_prices(145)
    cfg = small_config(horizons=(1,), max_folds=1, feature_profile='portable_price',
                       evaluation_tickers=DEFAULT_EVALUATION_TICKERS)
    first, second = tmp_path / 'first', tmp_path / 'second'
    a = compare_skill_modules(prices, cfg, holdout_dates=15, output=first)
    changed = prices.copy()
    reserved = sorted(prices.date.unique())[-15]
    mask = changed.date.ge(reserved) | changed.ticker.isin(['1216', '2002', '2412', 'IX0001'])
    changed.loc[mask, ['open', 'high', 'low', 'close', 'adj_close']] *= 3
    changed.loc[changed.ticker.isin(['1216', '2002', '2412', 'IX0001']), 'volume'] *= 100
    b = compare_skill_modules(changed, cfg, holdout_dates=15, output=second)
    for name in ['A', 'B']:
        pd.testing.assert_frame_equal(pd.read_parquet(first / name / 'predictions.parquet'),
                                      pd.read_parquet(second / name / 'predictions.parquet'))
    for name in ['C', 'D', 'R']:
        pd.testing.assert_frame_equal(pd.read_parquet(first / name / 'decisions.parquet'),
                                      pd.read_parquet(second / name / 'decisions.parquet'))
    assert a['paired_brier_B_minus_A'] == b['paired_brier_B_minus_A']


def test_no_signal_keeps_every_direction_and_empty_daily_portfolio(tmp_path):
    from twquant.skill_integration import compare_skill_modules
    prices, _ = expanded_prices(140)
    spec = dict(rules=[dict(metric='f_price_ma20', op='>', value=1e6)])
    report = compare_skill_modules(prices, small_config(horizons=(1,), max_folds=1),
        holdout_dates=15, rule_spec=spec, output=tmp_path / 'none')
    assert report['variants']['C']['portfolio']['orders'] == 0
    assert report['variants']['D']['portfolio']['orders'] == 0
    assert report['variants']['C']['direction_audit']['1']['coverage'] == 1
    assert report['variants']['C']['portfolio']['strategy']['total_return'] == 0


def test_outcome_rules_and_empty_prices_are_rejected():
    from twquant.skill_integration import compare_skill_modules
    prices, _ = expanded_prices(140)
    for column in ['target_1', 'next_open', 'f_fake_future']:
        with pytest.raises(ValueError, match='decision-time'):
            compare_skill_modules(prices, small_config(horizons=(1,), max_folds=1),
                rule_spec=dict(rules=[dict(metric=column, op='>', value=0)]), holdout_dates=15)
    with pytest.raises(ValueError):
        compare_skill_modules(prices.iloc[:0], small_config(), holdout_dates=15)


def test_risk_overlay_blocks_active_paper_plans_without_erasing_forecasts(tmp_path):
    from twquant.skill_integration import compare_skill_modules
    from twquant.skill_risk import RiskLimits
    prices, _ = expanded_prices(140)
    cfg = small_config(horizons=(1,), max_folds=1, min_probability=0., min_expected_return=-1.)
    report = compare_skill_modules(prices, cfg, holdout_dates=15, rule_spec={},
        risk_limits=RiskLimits(max_single_volatility=.000001), output=tmp_path / 'blocked')
    assert report['variants']['C']['portfolio']['orders'] > 0
    assert report['variants']['D']['portfolio']['orders'] == 0
    assert report['risk']['blocked'] == report['risk']['days']
    assert report['variants']['D']['prediction_metrics'] == report['variants']['A']['prediction_metrics']
