from itertools import combinations
from math import comb
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def constituent_rows(count=50, sessions=160):
    universe = tuple(str(6001 + i) for i in range(count))
    dates = pd.bdate_range('2024-01-01', periods=sessions)
    rows = pd.DataFrame([(d, t) for d in dates for t in universe], columns=['date', 'ticker'])
    rows['actual_return'] = np.repeat(np.resize([.01, -.01], sessions), count)
    rows['probability_up'] = np.where(rows.actual_return.gt(0), .8, .2)
    rows['horizon'], rows['model_version'], rows['decision_threshold'] = 1, 'shared-synthetic-model', .5
    return universe, rows


def test_full_fifty_audit_checks_all_subsets_exactly_without_materializing_two_million_rows():
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows()
    # Make five external stocks consistently wrong while preserving a high
    # aggregate score; every one of their five-stock subsets must be covered.
    bad = rows.ticker.isin(universe[:5])
    rows.loc[bad, 'probability_up'] = 1 - rows.loc[bad, 'probability_up']
    result = audit_constituent_accuracy(rows, universe, membership_as_of='2024-08-09',
                                        evidence_kind='synthetic', bootstrap_samples=20)
    assert result['constituent_count'] == 50
    assert result['combination_count'] == comb(50, 5) == 2_118_760
    assert result['all_combinations_enumerated'] is False
    assert result['five_stock_combinations'] == []
    assert result['overall_accuracy'] == pytest.approx(.9)
    assert result['worst_five_accuracy'] == 0
    assert set(result['worst_five']['tickers']) == set(universe[:5])
    assert result['empirical_target_met'] is False
    assert result['real_world_target_verified'] is False
    assert result['training_tickers'] == list(TRAINING_TICKERS)


def test_exact_worst_five_matches_exhaustive_evaluation_on_a_small_universe():
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows(8, 160)
    for i, ticker in enumerate(universe):
        wrong = rows.ticker.eq(ticker) & rows.date.isin(sorted(rows.date.unique())[:i * 10])
        rows.loc[wrong, 'probability_up'] = 1 - rows.loc[wrong, 'probability_up']
    result = audit_constituent_accuracy(rows, universe, membership_as_of='2024-08-09', bootstrap_samples=10)
    scores = []
    for subset in combinations(universe, 5):
        group = rows[rows.ticker.isin(subset)]
        scores.append(group.probability_up.ge(.5).eq(group.actual_return.gt(0)).mean())
    assert result['worst_five_accuracy'] == pytest.approx(min(scores))
    assert result['best_five']['accuracy'] == pytest.approx(max(scores))
    assert result['all_combinations_enumerated'] is True
    assert len(result['five_stock_combinations']) == comb(8, 5)


def test_full_membership_keeps_missing_assets_dates_and_abstentions_in_the_denominator():
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows()
    dates = rows.date.unique()
    rows = rows.loc[rows.ticker.ne(universe[0]) & rows.date.ne(dates[0])].copy()
    rows['abstain'] = rows.ticker.eq(universe[1])
    result = audit_constituent_accuracy(rows, universe, membership_as_of='2024-08-09',
                                        expected_dates=dates, bootstrap_samples=10)
    assert result['expected_predictions'] == 50 * len(dates)
    assert result['missing_predictions'] == len(dates) + 49
    assert result['coverage'] == pytest.approx(48 * (len(dates) - 1) / (50 * len(dates)))
    assert result['minimum_stock_accuracy'] == 0
    assert result['empirical_target_met'] is False


@pytest.mark.parametrize('column,value', [('model_version', 'another-model'), ('decision_threshold', .6)])
def test_constituent_audit_rejects_using_different_models_or_thresholds_for_external_stocks(column, value):
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows()
    rows.loc[0, column] = value
    with pytest.raises(ValueError, match='same shared model and threshold'):
        audit_constituent_accuracy(rows, universe, membership_as_of='2024-08-09')


def test_full_membership_rejects_duplicate_symbols_etfs_and_indices():
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows()
    for bad in (universe + universe[:1], universe + ('0050',), universe + ('IX0001',)):
        with pytest.raises(ValueError, match='constituent universe'):
            audit_constituent_accuracy(rows, bad, membership_as_of='2024-08-09')


def membership_document():
    universe, _ = constituent_rows()
    return dict(tickers=list(universe), count=50, as_of='2024/08/09',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',
        retrieved_at='2024-08-09T20:00:00+08:00', html_sha256='a' * 64)


def test_membership_loader_freezes_full_snapshot_metadata_and_does_not_accept_training_roles(tmp_path):
    from twquant.direction_audit import load_membership_snapshot
    document = membership_document()
    document.update(training_tickers=['6001'], unrelated_private_value='do-not-forward-this-field')
    path = tmp_path / 'membership.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    loaded = load_membership_snapshot(path)
    assert loaded['as_of'] == '2024-08-09'
    assert loaded['constituent_count'] == 50
    assert loaded['snapshot_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert loaded['historical_membership_verified'] is False
    assert 'training_tickers' not in loaded
    assert 'do-not-forward-this-field' not in json.dumps(loaded)


@pytest.mark.parametrize('change', [
    dict(tickers=['6001', '6002', '6003', '6004', '6005'], count=5),
    dict(count=49), dict(html_sha256='invalid'),
    dict(source_url='https://www.yuantaetfs.com/product/detail/0050/ratio?token=not-allowed'),
    dict(retrieved_at='2024-08-09T20:00:00'), dict(as_of='2024-08-09T12:00:00'),
])
def test_membership_loader_rejects_partial_or_inconsistent_snapshots(tmp_path, change):
    from twquant.direction_audit import load_membership_snapshot
    document = {**membership_document(), **change}
    path = tmp_path / 'membership.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ValueError):
        load_membership_snapshot(path)


def test_membership_loader_checks_adjacent_archived_html_when_present(tmp_path):
    from twquant.direction_audit import load_membership_snapshot
    document = membership_document()
    html = tmp_path / 'yuanta_0050.html'
    html.write_text('<html>synthetic test fixture</html>', encoding='utf-8')
    document['html_sha256'] = hashlib.sha256(html.read_bytes()).hexdigest()
    path = tmp_path / 'membership.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    assert load_membership_snapshot(path)['html_checksum_checked'] is True
    html.write_text('modified fixture', encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        load_membership_snapshot(path)


def test_full_constituent_research_uses_fixed_ten_selection_and_the_same_frozen_model(tmp_path):
    from dataclasses import replace
    from test_research import small_config
    from twquant.demo import make_demo_prices
    from twquant.direction import DirectionCandidate, search_direction_models
    from twquant.features import build_features, get_feature_columns
    prices = make_demo_prices(170)
    external = tuple(str(6001 + i) for i in range(40))
    expanded = [prices]
    for i, ticker in enumerate(external):
        copy = prices.loc[prices.ticker.eq('2330')].copy()
        copy['ticker'] = ticker
        copy[['open', 'high', 'low', 'close', 'adj_close', 'volume']] *= i + 2
        expanded.append(copy)
    core = build_features(prices)
    complete = build_features(pd.concat(expanded, ignore_index=True))
    pd.testing.assert_frame_equal(core[get_feature_columns(core)].reset_index(drop=True),
        complete.loc[complete.ticker.isin(prices.ticker.unique()), get_feature_columns(core)].reset_index(drop=True))
    cfg = small_config(horizons=(1,), max_folds=1, evaluation_tickers=('0050',))
    candidates = (DirectionCandidate('prior'), DirectionCandidate('logistic'))
    a = search_direction_models(core, cfg, candidates=candidates, holdout_dates=20, bootstrap_samples=10)
    full_cfg = replace(cfg, evaluation_tickers=('0050', *external))
    universe = (*TRAINING_TICKERS, *external)
    b = search_direction_models(complete, full_cfg, candidates=candidates, holdout_dates=20, bootstrap_samples=10,
        constituents=universe, membership_as_of='2025-12-31', evidence_kind='synthetic', output=tmp_path)
    pd.testing.assert_frame_equal(a.predictions, b.predictions)
    assert a.report['selections'] == b.report['selections']
    audit = b.report['constituents']['by_horizon']['1']
    assert audit['constituent_count'] == 50
    assert audit['combination_count'] == comb(50, 5)
    assert audit['expected_predictions'] == 50 * b.report['by_horizon']['1']['test_dates']
    assert audit['coverage'] == 1
    assert b.report['constituents']['selection_scope'] == 'fixed_ten_only'
    saved = pd.read_parquet(tmp_path / 'constituent_predictions.parquet')
    assert set(saved.ticker) == set(universe)
    assert saved.groupby('date').model_version.nunique().eq(1).all()
    assert saved.groupby('date').decision_threshold.nunique().eq(1).all()
    assert not saved.ticker.eq('0050').any()
    changed = complete.copy()
    mask = changed.ticker.isin(external)
    changed.loc[mask, 'target_1'] = -.9
    changed.loc[mask, 'f_return_1'] = 999.
    c = search_direction_models(changed, full_cfg, candidates=candidates, holdout_dates=20, bootstrap_samples=10,
        constituents=universe, membership_as_of='2025-12-31', evidence_kind='synthetic')
    pd.testing.assert_frame_equal(b.predictions, c.predictions)
    assert b.report['selections'] == c.report['selections']
    partial = complete.loc[complete.ticker.ne(external[0])]
    d = search_direction_models(partial, full_cfg, candidates=candidates, holdout_dates=20, bootstrap_samples=10,
        constituents=universe, membership_as_of='2025-12-31', evidence_kind='synthetic')
    partial_audit = d.report['constituents']['by_horizon']['1']
    assert partial_audit['missing_predictions'] == partial_audit['test_dates']
    assert partial_audit['coverage'] == pytest.approx(49 / 50)
    assert partial_audit['empirical_target_met'] is False
    with pytest.raises(ValueError, match='evaluation-only'):
        search_direction_models(complete, cfg, candidates=candidates, holdout_dates=20,
            constituents=universe, membership_as_of='2025-12-31')


def test_full_synthetic_success_stays_distinct_from_real_market_verification():
    from twquant.direction_audit import audit_constituent_accuracy
    universe, rows = constituent_rows()
    audit = audit_constituent_accuracy(rows, universe, membership_as_of='2024-08-09',
                                       evidence_kind='synthetic', bootstrap_samples=10)
    assert audit['empirical_target_met'] is True
    assert audit['simulated_target_met'] is True
    assert audit['real_world_target_verified'] is False
