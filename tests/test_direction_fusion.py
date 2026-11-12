import numpy as np
import pandas as pd
import pytest

from twquant.direction import DirectionCandidate
from twquant.direction_models import fit_direction_model
from twquant.universe import TRAINING_TICKERS


def training_panel():
    rng = np.random.default_rng(613)
    dates = pd.bdate_range('2020-01-02', periods=120)
    panels = []
    for ticker in TRAINING_TICKERS:
        us = rng.normal(size=len(dates))
        panels.append(pd.DataFrame(dict(
            ticker=ticker, date=dates,
            decision_ts=dates.tz_localize('Asia/Taipei').tz_convert('UTC'),
            f_return_1=rng.normal(size=len(dates)),
            f_ext_fmp_spy_return1=us,
            f_ext_taifex_tx_night_return=rng.normal(size=len(dates)),
            target_1=np.where(us > 0, .01, -.01),
            label_end_1=pd.Series(dates).shift(-1).to_numpy(),
            label_available_at_1=pd.Series(dates).shift(-1).dt.tz_localize('Asia/Taipei').dt.tz_convert('UTC'),
        )))
    return pd.concat(panels, ignore_index=True).dropna(subset=['label_end_1'])


def test_fusion_learns_source_weights_on_later_purged_training_dates():
    panel = training_panel()
    columns = [c for c in panel if c.startswith('f_')]
    model, used = fit_direction_model(DirectionCandidate('fusion_learned', 'portable_market'),
                                      panel, columns, 1, 42)
    details = model.training_details
    assert details['weights']['us'] > details['weights']['price']
    assert details['weights']['us'] > details['weights']['night']
    assert sum(details['weights'].values()) == pytest.approx(1.)
    assert min(details['weights'].values()) >= 0
    assert pd.Timestamp(details['expert_label_available_max']) < pd.Timestamp(details['weight_decision_min'])
    assert pd.Timestamp(details['expert_label_end_max']) < pd.Timestamp(details['weight_start'])
    assert len(used) < len(panel)  # inner-purge rows are not reported as fitted
    probe = pd.DataFrame({c: [-1., 1.] for c in columns})
    probability = model.predict_proba(probe)[:, 1]
    assert probability[0] < .35 and probability[1] > .65


def test_fusion_rejects_unsupported_profiles_instead_of_ignoring_news_inputs():
    with pytest.raises(ValueError, match='portable_market'):
        DirectionCandidate('fusion_learned', 'portable_all')


def test_equal_fusion_missing_sources_use_prior_without_redistribution():
    panel = training_panel()
    columns = [c for c in panel if c.startswith('f_')]
    panel['f_ext_fmp_spy_return1_age_days'] = 1.
    columns.append('f_ext_fmp_spy_return1_age_days')
    model, _ = fit_direction_model(DirectionCandidate('fusion_equal', 'portable_market'), panel, columns, 1, 42)
    assert list(model.training_details['weights'].values()) == [.25] * 4
    probe = panel.iloc[:3].copy()
    probe[['f_ext_fmp_spy_return1', 'f_ext_taifex_tx_night_return']] = np.nan
    probabilities = model.source_probabilities(probe)
    np.testing.assert_array_equal(probabilities.us, np.full(3, model.prior))
    np.testing.assert_array_equal(probabilities.night, np.full(3, model.prior))
    np.testing.assert_allclose(model.contributions(probe).sum(axis=1), model.predict_proba(probe)[:, 1])
    probe[columns] = np.nan
    np.testing.assert_allclose(model.predict_proba(probe)[:, 1], model.prior)


def test_fusion_queries_cannot_change_shared_weights_or_transforms():
    panel = training_panel()
    columns = [c for c in panel if c.startswith('f_')]
    candidate = DirectionCandidate('fusion_learned', 'portable_market')
    model, _ = fit_direction_model(candidate, panel, columns, 1, 42)
    probe = panel.iloc[:4].assign(ticker='1216', target_1=999.)
    original = model.predict_proba(probe)
    extreme = probe.assign(**{c: 1e12 for c in columns})
    combined = pd.concat([extreme, probe, extreme], ignore_index=True)
    np.testing.assert_array_equal(model.predict_proba(combined)[4:8], original)
    np.testing.assert_array_equal(model.predict_proba(probe.assign(ticker='0050', target_1=-999.)), original)
    with pytest.raises(ValueError, match='fixed ten'):
        fit_direction_model(candidate, panel.assign(ticker='1216'), columns, 1, 42)


def test_fusion_never_uses_labels_at_the_inner_maturity_boundary():
    panel = training_panel()
    columns = [c for c in panel if c.startswith('f_')]
    candidate = DirectionCandidate('fusion_learned', 'portable_market')
    model, used = fit_direction_model(candidate, panel, columns, 1, 42)
    purged = ~panel.index.isin(used.index)
    assert purged.any()
    changed = panel.copy()
    changed.loc[purged, 'target_1'] *= -1
    replay, _ = fit_direction_model(candidate, changed, columns, 1, 42)
    np.testing.assert_array_equal(model.predict_proba(panel), replay.predict_proba(panel))


def test_fusion_records_unavailable_groups_and_constant_class_fallback():
    panel = training_panel()
    columns = [c for c in panel if c.startswith('f_')]
    panel[['f_ext_fmp_spy_return1', 'f_ext_taifex_tx_night_return']] = np.nan
    panel['target_1'] = -.01
    model, _ = fit_direction_model(DirectionCandidate('fusion_learned', 'portable_market'), panel, columns, 1, 42)
    assert model.training_details['absent_groups'] == ['us', 'night']
    np.testing.assert_array_equal(model.predict_proba(panel)[:, 1], np.zeros(len(panel)))


def test_reference_fingerprint_binds_source_fusion_implementation():
    from twquant.direction_reference import _recipe_signature
    assert 'direction_fusion.py' in _recipe_signature()['recipe_files']


def test_outer_search_keeps_fusion_selection_independent_of_future_and_external_labels():
    from twquant.direction import FUSION_CANDIDATES, search_direction_models
    from twquant.research import ResearchConfig
    from twquant.comparison import reserve_development
    from twquant.research import temporal_splits
    from twquant.features import get_feature_columns
    from twquant.research import _validate
    panel = training_panel()
    panel['decision_ts'] += pd.Timedelta(hours=20)
    panel['label_available_at_1'] += pd.Timedelta(hours=20)
    frame = pd.concat([panel, *[panel.loc[panel.ticker.eq(TRAINING_TICKERS[0])].assign(ticker=t)
                               for t in ('0050', '1216')]], ignore_index=True)
    cfg = ResearchConfig(horizons=(1,), min_train_dates=60, calibration_dates=10, test_dates=10,
                         max_folds=1, evaluation_tickers=('1216',))
    a = search_direction_models(frame, cfg, candidates=FUSION_CANDIDATES, holdout_dates=10, bootstrap_samples=5)
    development, boundary = reserve_development(frame, (1,), 10)
    fixed = _validate(development, get_feature_columns(development), cfg)
    split = temporal_splits(fixed.drop(columns=['benchmark_target_1'], errors='ignore'), 1, cfg)[0]
    test_dates = fixed.loc[split['test'], 'date'].unique()
    changed = frame.copy()
    changed.loc[changed.date.isin(test_dates) | changed.date.ge(boundary) | changed.ticker.eq('1216'), 'target_1'] *= -1
    changed.loc[changed.ticker.eq('1216'), 'f_ext_fmp_spy_return1'] = 1e9
    b = search_direction_models(changed, cfg, candidates=FUSION_CANDIDATES, holdout_dates=10, bootstrap_samples=5)
    pd.testing.assert_frame_equal(a.predictions.drop(columns='actual_return'), b.predictions.drop(columns='actual_return'))
    assert a.report['selections'] == b.report['selections']
    assert set(a.transfer_predictions.model_version) == set(a.predictions.model_version)


def test_reproducible_study_runner_freezes_inputs_and_refuses_overwrite(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'research_fusion.py'
    assert script.exists(), 'Source-fusion study must have an executable reproducibility entry point'
    panel = training_panel()
    panel['decision_ts'] += pd.Timedelta(hours=20)
    panel['label_available_at_1'] += pd.Timedelta(hours=20)
    panel = pd.concat([panel, panel.loc[panel.ticker.eq(TRAINING_TICKERS[0])].assign(ticker='0050')], ignore_index=True)
    path = tmp_path / 'panel.parquet'
    panel.to_parquet(path)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(horizons=[1], min_train_dates=60, calibration_dates=10,
                                     test_dates=10, max_folds=1)))
    args = [sys.executable, str(script), '--frame', str(path), '--config', str(config),
            '--out', str(tmp_path / 'study'), '--holdout-dates', '10', '--mode', 'postclose',
            '--evidence-kind', 'synthetic']
    result = subprocess.run(args, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    protocol = json.loads((tmp_path / 'study' / 'protocol.json').read_text())
    summary = json.loads((tmp_path / 'study' / 'comparison.json').read_text())
    assert protocol['evidence_kind'] == 'synthetic'
    assert summary['real_world_target_verified'] is False
    assert len(summary['arms']) == 6
    assert summary['verification']['source_and_inputs_unchanged']
    assert subprocess.run(args, capture_output=True).returncode != 0
