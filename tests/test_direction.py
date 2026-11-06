import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def direction_rows(sessions=160):
    dates = pd.bdate_range('2024-01-01', periods=sessions)
    rows = pd.DataFrame([(d, t) for d in dates for t in TRAINING_TICKERS], columns=['date', 'ticker'])
    rows['actual_return'] = np.tile([.01, -.01], len(rows) // 2)
    rows['probability_up'] = np.where(rows.actual_return > 0, .8, .2)
    rows['horizon'] = 1
    return rows


def test_any_five_audit_checks_all_252_combinations_and_every_stock():
    from twquant.direction import audit_five_stock_accuracy
    rows = direction_rows()
    # A high overall result must not hide a single consistently wrong stock.
    bad = rows.ticker == TRAINING_TICKERS[0]
    rows.loc[bad, 'probability_up'] = 1 - rows.loc[bad, 'probability_up']
    report = audit_five_stock_accuracy(rows, evidence_kind='historical', bootstrap_samples=30)
    assert len(report['five_stock_combinations']) == 252
    assert report['overall_accuracy'] == pytest.approx(.9)
    assert report['worst_five_accuracy'] == pytest.approx(.8)
    assert report['minimum_stock_accuracy'] == 0
    assert report['empirical_target_met'] is False
    assert report['real_world_target_verified'] is False


def test_missing_predictions_and_abstentions_stay_in_the_denominator():
    from twquant.direction import audit_five_stock_accuracy
    rows = direction_rows()
    dates = rows.date.unique()
    rows = rows.loc[rows.ticker != TRAINING_TICKERS[0]].copy()
    rows['abstain'] = rows.ticker == TRAINING_TICKERS[1]
    report = audit_five_stock_accuracy(rows, expected_dates=dates, bootstrap_samples=30)
    assert report['overall_accuracy'] == pytest.approx(.8)
    assert report['coverage'] == pytest.approx(.8)
    assert report['expected_predictions'] == len(dates) * 10
    assert report['empirical_target_met'] is False


def test_synthetic_success_never_becomes_real_evidence_and_one_sided_baseline_is_visible():
    from twquant.direction import audit_five_stock_accuracy
    rows = direction_rows()
    # Both directions occur for each ticker over time.
    rows['actual_return'] = np.repeat(np.resize([.01, -.01], 160), 10)
    rows['probability_up'] = np.where(rows.actual_return > 0, .8, .2)
    report = audit_five_stock_accuracy(rows, evidence_kind='synthetic', bootstrap_samples=30)
    assert report['empirical_target_met'] is True
    assert report['simulated_target_met'] is True
    assert report['real_world_target_verified'] is False
    rows['actual_return'] = .01
    rows['probability_up'] = .8
    report = audit_five_stock_accuracy(rows, bootstrap_samples=30)
    assert report['overall_accuracy'] == 1
    assert report['directional_edge_demonstrated'] is False
    assert report['empirical_target_met'] is False


def test_audit_rejects_duplicates_external_assets_and_mixed_horizons():
    from twquant.direction import audit_five_stock_accuracy
    rows = direction_rows()
    with pytest.raises(ValueError, match='duplicate'):
        audit_five_stock_accuracy(pd.concat([rows, rows.iloc[:1]]))
    with pytest.raises(ValueError, match='universe'):
        audit_five_stock_accuracy(rows.assign(ticker='0050'))
    rows.loc[0, 'horizon'] = 5
    with pytest.raises(ValueError, match='one horizon'):
        audit_five_stock_accuracy(rows)


def search_frame():
    from test_research import research_frame
    base = research_frame().query("ticker == '2330'").copy()
    rows = []
    for ticker in (*TRAINING_TICKERS, '0050', '1216'):
        f = base.assign(ticker=ticker)
        for h in (1, 5, 20):
            f[f'label_available_at_{h}'] = (f[f'label_end_{h}'].dt.tz_localize('Asia/Taipei')
                                                   + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


@pytest.mark.parametrize('selection_policy', ['worst_stock_accuracy', 'pooled_brier', 'worst_stock_brier'])
def test_search_selection_ignores_outer_test_labels_reserved_labels_and_transfer_assets(tmp_path, selection_policy):
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    from twquant.comparison import reserve_development
    from twquant.research import temporal_splits
    frame = search_frame()
    cfg = small_config(horizons=(1,), max_folds=1, evaluation_tickers=('0050', '1216'))
    candidates = (DirectionCandidate('logistic'), DirectionCandidate('prior'))
    a = search_direction_models(frame, cfg, candidates=candidates, holdout_dates=15,
                                bootstrap_samples=20, output=tmp_path, selection_policy=selection_policy)
    dev, start = reserve_development(frame, cfg.horizons, 15)
    fixed = dev.loc[dev.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True)
    split = temporal_splits(fixed, 1, cfg)[0]
    test_dates = fixed.loc[split['test'], 'date'].unique()
    changed = frame.copy()
    changed.loc[changed.date.isin(test_dates) | (changed.date >= start), 'target_1'] = .8
    changed.loc[changed.ticker.isin(['0050', '1216']), 'f_signal'] = 900
    changed.loc[changed.ticker.isin(['0050', '1216']), 'target_1'] = -.9
    b = search_direction_models(changed, cfg, candidates=candidates, holdout_dates=15,
                                bootstrap_samples=20, selection_policy=selection_policy)
    pd.testing.assert_frame_equal(a.predictions.drop(columns='actual_return'), b.predictions.drop(columns='actual_return'))
    assert a.report['selections'] == b.report['selections']
    assert a.report['holdout_opened'] is False
    assert set(a.predictions.ticker) == set(TRAINING_TICKERS)
    assert set(a.transfer_predictions.ticker) == {'0050', '1216'}
    assert set(a.transfer_predictions.date) == set(a.predictions.date)
    assert set(a.transfer_predictions.model_version) == set(a.predictions.model_version)
    thresholds = 5 if selection_policy == 'worst_stock_accuracy' else 1
    assert len(a.report['selections'][0]['candidate_scores']) == 2 * thresholds
    assert a.report['protocol']['selection_policy'] == selection_policy
    if selection_policy != 'worst_stock_accuracy':
        assert a.predictions.decision_threshold.eq(.5).all()
        scores = a.report['selections'][0]['candidate_scores']
        field = 'brier' if selection_policy == 'pooled_brier' else 'worst_stock_brier'
        chosen = next(s for s in scores if s['candidate'] == a.predictions.candidate.iloc[0])
        assert chosen[field] == min(s[field] for s in scores)
    assert (tmp_path / 'protocol.json').exists()
    assert (tmp_path / 'predictions.parquet').exists()
    selected = a.report['selections'][0]
    assert pd.Timestamp(selected['train_label_end_max']) < pd.Timestamp(selected['calibration_start'])
    assert pd.Timestamp(selected['calibration_label_end_max']) < pd.Timestamp(selected['test_start'])


def test_direction_search_requires_close_return_and_unique_candidate_names():
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    frame = search_frame()
    with pytest.raises(ValueError, match='close_return'):
        search_direction_models(frame, small_config(objective='open_return'))
    with pytest.raises(ValueError, match='unique'):
        search_direction_models(frame, small_config(), candidates=(DirectionCandidate('prior'),) * 2)


def test_search_retains_benchmark_sessions_when_all_ten_stock_rows_are_missing():
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    frame = search_frame()
    cfg = small_config(horizons=(1,), max_folds=1, test_dates=20)
    a = search_direction_models(frame, cfg, holdout_dates=15,
        candidates=(DirectionCandidate('prior'),), bootstrap_samples=10)
    absent = sorted(a.predictions.date.unique())[5]
    frame = frame.loc[~(frame.date.eq(absent) & frame.ticker.isin(TRAINING_TICKERS))]
    result = search_direction_models(frame, cfg, holdout_dates=15,
        candidates=(DirectionCandidate('prior'),), bootstrap_samples=10)
    audit = result.report['by_horizon']['1']
    assert audit['missing_predictions'] == 10
    assert audit['coverage'] < 1


@pytest.mark.parametrize('model', ['factor_ridge', 'factor_hist'])
def test_market_and_stock_residual_model_learns_both_signals_without_test_or_transfer_refitting(model):
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    frame = search_frame()
    rng = np.random.default_rng(63)
    dates = sorted(frame.date.unique())
    market = dict(zip(dates, rng.normal(size=len(dates))))
    frame['f_market_return_1'] = frame.date.map(market)
    frame['f_signal'] = rng.normal(size=len(frame))
    frame['target_1'] = .02 * (frame.f_market_return_1 + frame.f_signal) + rng.normal(0, .0001, len(frame))
    frame.loc[frame.label_end_1.isna(), 'target_1'] = np.nan
    cfg = small_config(horizons=(1,), test_dates=30, max_folds=1, evaluation_tickers=('0050', '1216'))
    candidate = (DirectionCandidate(model),)
    a = search_direction_models(frame, cfg, candidates=candidate, holdout_dates=15,
                                bootstrap_samples=10, selection_policy='pooled_brier')
    valid = a.predictions.actual_return.notna()
    accuracy = a.predictions.probability_up[valid].ge(.5).eq(a.predictions.actual_return[valid].gt(0)).mean()
    assert accuracy > .85
    details = a.report['selections'][0]['model_details']
    assert details['market_features'] == ['f_market_return_1']
    assert details['market_training_dates'] == a.report['selections'][0]['training_dates']
    assert details['residual_training_rows'] == details['market_training_dates'] * 10
    assert set(a.transfer_predictions.model_version) == set(a.predictions.model_version)
    changed = frame.copy()
    changed.loc[changed.date.isin(a.predictions.date), 'target_1'] = -.9
    changed.loc[changed.ticker.isin(['0050', '1216']), ['f_signal', 'f_market_return_1']] = 999.
    changed.loc[changed.ticker.isin(['0050', '1216']), 'target_1'] = -.9
    b = search_direction_models(changed, cfg, candidates=candidate, holdout_dates=15,
                                bootstrap_samples=10, selection_policy='pooled_brier')
    pd.testing.assert_frame_equal(a.predictions.drop(columns='actual_return'), b.predictions.drop(columns='actual_return'))
    assert a.report['selections'] == b.report['selections']


def test_incomplete_market_history_skips_factor_candidate_and_keeps_other_candidates():
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    frame = search_frame().loc[lambda f: f.ticker.ne(TRAINING_TICKERS[0])]
    cfg = small_config(horizons=(1,), max_folds=1)
    candidates = (DirectionCandidate('prior'), DirectionCandidate('factor_ridge'))
    result = search_direction_models(frame, cfg, candidates=candidates, holdout_dates=15,
                                    bootstrap_samples=10, selection_policy='pooled_brier')
    assert result.predictions.candidate.str.startswith('prior:').all()
    scores = result.report['selections'][0]['candidate_scores']
    assert scores[-1]['status'] == 'unavailable'
    assert scores[-1]['reason'] == 'incomplete_fixed_ten_market_history'
    assert result.report['total_selection_trials'] == 1
    with pytest.raises(ValueError, match='No eligible direction candidates'):
        search_direction_models(frame, cfg, candidates=candidates[1:], holdout_dates=15,
                                bootstrap_samples=10, selection_policy='pooled_brier')


def test_information_scenario_has_explicit_hypotheses_mature_labels_and_delay():
    from twquant.direction import make_information_scenario
    a, metadata = make_information_scenario(sessions=180, strength=3., seed=7)
    b, _ = make_information_scenario(sessions=180, strength=3., seed=7, delay_dates=1)
    assert metadata['evidence_kind'] == 'synthetic'
    assert metadata['provider_data_obtained'] is False
    assert metadata['idealized_sign_accuracy'] == pytest.approx(.5 + np.arctan(3) / np.pi)
    assert set(a.ticker) == set(TRAINING_TICKERS) | {'0050'}
    stock = a.loc[a.ticker == '2330'].reset_index(drop=True)
    delayed = b.loc[b.ticker == '2330'].reset_index(drop=True)
    np.testing.assert_allclose(stock.f_hypothetical_information.iloc[:-1], delayed.f_hypothetical_information.iloc[1:])
    assert pd.isna(delayed.f_hypothetical_information.iloc[0])
    assert stock.target_1.iloc[-1:].isna().all()
    assert (stock.label_available_at_1.iloc[:-1] > stock.decision_ts.iloc[:-1]).all()
    pd.testing.assert_series_equal(stock.target_1, delayed.target_1)


def test_information_simulation_records_all_seeds_instead_of_picking_a_lucky_run(tmp_path):
    from twquant.direction import simulate_information_scenarios
    report = simulate_information_scenarios(sessions=380, seeds=(7, 11), output=tmp_path)
    assert report['evidence_kind'] == 'synthetic'
    assert report['real_world_target_verified'] is False
    assert len(report['experiments']) == 5 * 2
    assert all(s['seeds_tested'] == [7, 11] for s in report['scenarios'].values())
    assert report['scenarios']['informative']['all_seeds_simulated_target_met'] is True
    assert report['scenarios']['no_information']['all_seeds_simulated_target_met'] is False
    assert (tmp_path / 'report.json').exists()
