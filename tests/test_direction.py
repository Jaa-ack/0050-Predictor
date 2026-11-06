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


def test_search_selection_ignores_outer_test_labels_reserved_labels_and_transfer_assets(tmp_path):
    from test_research import small_config
    from twquant.direction import DirectionCandidate, search_direction_models
    from twquant.comparison import reserve_development
    from twquant.research import temporal_splits
    frame = search_frame()
    cfg = small_config(horizons=(1,), max_folds=1, evaluation_tickers=('0050', '1216'))
    candidates = (DirectionCandidate('logistic'), DirectionCandidate('prior'))
    a = search_direction_models(frame, cfg, candidates=candidates, holdout_dates=15,
                                bootstrap_samples=20, output=tmp_path)
    dev, start = reserve_development(frame, cfg.horizons, 15)
    fixed = dev.loc[dev.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True)
    split = temporal_splits(fixed, 1, cfg)[0]
    test_dates = fixed.loc[split['test'], 'date'].unique()
    changed = frame.copy()
    changed.loc[changed.date.isin(test_dates) | (changed.date >= start), 'target_1'] = .8
    changed.loc[changed.ticker.isin(['0050', '1216']), 'f_signal'] = 900
    changed.loc[changed.ticker.isin(['0050', '1216']), 'target_1'] = -.9
    b = search_direction_models(changed, cfg, candidates=candidates, holdout_dates=15,
                                bootstrap_samples=20)
    pd.testing.assert_frame_equal(a.predictions.drop(columns='actual_return'), b.predictions.drop(columns='actual_return'))
    assert a.report['selections'] == b.report['selections']
    assert a.report['holdout_opened'] is False
    assert set(a.predictions.ticker) == set(TRAINING_TICKERS)
    assert set(a.transfer_predictions.ticker) == {'0050', '1216'}
    assert set(a.transfer_predictions.date) == set(a.predictions.date)
    assert set(a.transfer_predictions.model_version) == set(a.predictions.model_version)
    assert len(a.report['selections'][0]['candidate_scores']) == 2 * 5
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
