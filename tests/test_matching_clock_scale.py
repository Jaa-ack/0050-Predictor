import importlib.util
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from twquant.matching_simulation import MatchingScenario, simulate_matching_panel


def scale(frame, slowdown):
    assert importlib.util.find_spec('twquant.matching_clock_scale') is not None, 'Synthetic matching clock scaling is missing'
    from twquant.matching_clock_scale import scale_matching_clock
    return scale_matching_clock(frame, slowdown=slowdown)


def fixture():
    scenario = MatchingScenario('clock', initial_depth_range=(50, 100),
        market_fraction=.15, cancel_fraction=.35, latency_seconds=.001,
        queue={'flow_imbalance': .5}, external_event_rates=(('1216', 1000.),))
    days = pd.bdate_range('2020-01-02', periods=12)
    frame = simulate_matching_panel(('2330', '1216', 'IX0001'), days, scenario, seed=17, future_seed=29)
    return scenario, days, frame


def test_clock_scaling_matches_direct_lower_rate_simulation_with_same_emission_state():
    scenario, days, original = fixture()
    saved = original.copy(deep=True)
    derived = scale(original, 100)
    direct = simulate_matching_panel(('2330', '1216', 'IX0001'), days,
        replace(scenario, event_rate=1., latency_seconds=.1,
                external_event_rates=(('1216', 10.),)), seed=17, future_seed=29)
    columns = [c for c in original if c.startswith(('f_book_', 'f_event_', 'f_trade_', 'observed_'))]
    columns += ['actual_return', 'timeout', 'simulation_resolved', 'input_ready',
        'prediction_at', 'decision_at', 'target_at', 'label_available_at',
        'max_input_available_at', 'event_rate', 'latency_seconds', 'first_change_price',
        'first_change_fill_rank', 'diagnostic_anchor_trade_price',
        'diagnostic_pre_emission_events', 'diagnostic_pre_emission_price_changes']
    pd.testing.assert_frame_equal(derived[columns], direct[columns])
    hit = derived.actual_return.notna() & derived.actual_return.ne(0)
    error = (derived.loc[hit, 'first_change_at'] - direct.loc[hit, 'first_change_at']).dt.total_seconds().to_numpy() * 1e9
    assert np.abs(error).max(initial=0.) <= 101
    assert len(derived) == len(original) and not derived.duplicated(['ticker', 'decision_at']).any()
    pd.testing.assert_frame_equal(original, saved)


def test_later_first_changes_become_verified_timeouts_without_dropping_any_stock():
    _, _, frame = fixture()
    derived = scale(frame, 1000)
    original_hit = frame.actual_return.notna() & frame.actual_return.ne(0)
    late = original_hit & (frame.first_change_at-frame.decision_at).gt(pd.Timedelta(seconds=.06))
    assert late.any()
    assert derived.loc[late, 'timeout'].all() and derived.loc[late, 'actual_return'].eq(0).all()
    assert derived.loc[late, ['first_change_at', 'first_change_price', 'first_change_fill_rank']].isna().all().all()
    assert derived.loc[late, 'simulation_resolved'].all()
    idx = frame.ticker.eq('IX0001')
    assert derived.loc[idx, 'actual_return'].isna().all() and not derived.loc[idx, 'timeout'].any()
    assert len(derived) == len(frame)
    assert 'diagnostic_events_applied' not in derived and 'reference_events_applied' in derived


def test_source_unknown_answers_stay_unknown_even_after_slowing():
    _, _, frame = fixture()
    row = frame.index[frame.ticker.eq('2330')][0]
    frame.loc[row, ['actual_return', 'first_change_price', 'first_change_fill_rank']] = np.nan
    frame.loc[row, 'first_change_at'] = pd.NaT
    frame.loc[row, 'simulation_resolved'] = False
    frame.loc[row, 'timeout'] = False
    derived = scale(frame, 100)
    assert pd.isna(derived.loc[row, 'actual_return'])
    assert not derived.loc[row, 'simulation_resolved'] and not derived.loc[row, 'timeout']
    assert len(derived) == len(frame)


@pytest.mark.parametrize('slowdown', [0, -1, True, 1.5, 1001])
def test_clock_scaling_requires_a_bounded_positive_integer_slowdown(slowdown):
    _, _, frame = fixture()
    with pytest.raises(ValueError, match='[Ss]lowdown'):
        scale(frame, slowdown)


def test_clock_scaling_rejects_real_sources_and_non_event_history_units():
    _, _, frame = fixture()
    for changed in (frame.assign(source='fubon'), frame.assign(history_window_unit='seconds')):
        with pytest.raises(ValueError, match='synthetic|events'):
            scale(changed, 100)


def parent_study(tmp_path):
    import json
    from twquant.queue_first_study import run_matching_study
    from twquant.universe import TRAINING_TICKERS
    members = list(TRAINING_TICKERS) + [str(4000+i) for i in range(40)]
    membership = tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members, count=50, as_of='2026-11-06',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',
        retrieved_at='2026-11-07T00:00:00+00:00', html_sha256='a'*64)))
    config = dict(train_dates=30, calibration_dates=10, test_dates=126, seeds=[17],
        max_wait_seconds=60, max_events=2048, candidates=['spread_bounce'],
        scenarios=[dict(name='canonical', initial_depth_range=[50,100],
                        market_fraction=.15, cancel_fraction=.35, latency_seconds=.001,
                        queue={'flow_imbalance':.5}),
                   dict(name='thin', initial_depth_range=[5,10], market_fraction=.15,
                        cancel_fraction=.35, latency_seconds=.001, queue={'flow_imbalance':.5})])
    reference = tmp_path/'reference'
    run_matching_study(config, membership, reference)
    return reference


def test_clock_transfer_study_uses_one_primary_model_for_complete_daily_grids(tmp_path):
    from twquant import matching_clock_scale as module
    assert hasattr(module, 'run_clock_transfer_study'), 'Frozen-model clock transfer study is missing'
    reference = parent_study(tmp_path)
    report = module.run_clock_transfer_study(reference,
        dict(slowdowns=[1,100], training_scenario='canonical'), tmp_path/'derived')
    assert report['models_trained']==1 and len(report['experiments'])==4
    versions = set()
    for run in report['experiments']:
        audit = run['audit']
        assert audit['expected_predictions']==50*126 and audit['prediction_coverage']==1
        assert audit['missing_predictions']==audit['invalid_predictions']==0
        assert run['transfer']['IX0001']['unresolved_answers']==126
        assert run['transfer']['IX0001']['accuracy'] is None
        versions.add(run['model_version'])
    assert len(versions)==1 and not report['empirical_target_met']
    detail = report['training_details']['17']
    from twquant.universe import TRAINING_TICKERS
    assert set(detail['training_tickers'])==set(TRAINING_TICKERS) and detail['training_rows']==300
    assert not detail['external_targets_used']


def test_clock_transfer_rejects_changed_reference_files_before_creating_output(tmp_path):
    import json
    from twquant import matching_clock_scale as module
    assert hasattr(module, 'run_clock_transfer_study'), 'Frozen-model clock transfer study is missing'
    reference = parent_study(tmp_path)
    run = json.loads((reference/'report.json').read_text())['experiments'][0]
    path = reference/run['predictions']
    path.write_bytes(path.read_bytes()+b'changed')
    output = tmp_path/'rejected'
    with pytest.raises(ValueError, match='fingerprint'):
        module.run_clock_transfer_study(reference,
            dict(slowdowns=[1,100], training_scenario='canonical'), output)
    assert not output.exists()


def test_five_minute_clock_target_matches_direct_replay_and_preserves_sixty_seconds():
    from twquant.matching_clock_scale import scale_matching_clock
    scenario, days, original = fixture()
    sixty = scale(original, 1000)
    longer = scale_matching_clock(original, slowdown=1000, max_wait_seconds=300)
    direct = simulate_matching_panel(('2330', '1216', 'IX0001'), days,
        replace(scenario, event_rate=.1, latency_seconds=1.,
                external_event_rates=(('1216', 1.),)), seed=17, future_seed=29,
        max_wait_seconds=300)
    columns = ['actual_return', 'timeout', 'simulation_resolved', 'input_ready',
        'target_at', 'label_available_at', 'max_wait_seconds', 'first_change_price',
        'first_change_fill_rank', 'diagnostic_anchor_trade_price']
    pd.testing.assert_frame_equal(longer[columns], direct[columns])
    hit = longer.actual_return.notna() & longer.actual_return.ne(0)
    difference = (longer.loc[hit, 'first_change_at']-direct.loc[hit, 'first_change_at']).dt.total_seconds()*1e9
    assert difference.abs().max() <= 1001
    assert (sixty.timeout & ~longer.timeout & hit).any()
    assert len(longer)==len(sixty)==len(original)
    assert original.max_wait_seconds.eq(60).all() and sixty.max_wait_seconds.eq(60).all()
    assert longer.loc[longer.ticker.eq('IX0001'), 'actual_return'].isna().all()


def test_five_minute_target_cannot_invent_unobserved_reference_future():
    from twquant.matching_clock_scale import scale_matching_clock
    _, _, original = fixture()
    with pytest.raises(ValueError, match='reference|covered'):
        scale_matching_clock(original, slowdown=1, max_wait_seconds=300)


def test_five_minute_wait_is_accepted_only_for_transaction_first_changes():
    from twquant.first_change import _wait
    from twquant.trade_first_change import TARGET
    _wait(300, target_kind=TARGET)
    for target in (None, 'first_mid_change_before_deadline'):
        with pytest.raises(ValueError):
            _wait(300, target_kind=target)
    with pytest.raises(ValueError):
        _wait(True, target_kind=TARGET)


def test_clock_transfer_audits_the_five_minute_target_separately(tmp_path):
    from twquant.matching_clock_scale import run_clock_transfer_study
    reference = parent_study(tmp_path)
    report = run_clock_transfer_study(reference,
        dict(slowdowns=[100,1000], training_scenario='canonical', max_wait_seconds=300),
        tmp_path/'five_minutes')
    assert len(report['experiments'])==4 and report['models_trained']==1
    assert len({r['model_version'] for r in report['experiments']})==1
    for run in report['experiments']:
        assert run['audit']['max_wait_seconds']==300
        assert run['audit']['expected_predictions']==50*126
        assert run['audit']['prediction_coverage']==1
        assert run['transfer']['IX0001']['accuracy'] is None


def test_reference_compatibility_allows_only_the_specific_trade_deadline_extension():
    from twquant.matching_clock_scale import _reference_module_compatible
    old = 'def _wait(seconds):\n    if type(seconds) is not int or seconds not in (1,5,30,60):raise ValueError("wait")\n\ndef use(x):\n    _wait(x)\n'
    new = old.replace('def _wait(seconds):', 'def _wait(seconds, *, target_kind=None):\n    if target_kind=="first_trade_change_before_deadline" and type(seconds) is int and seconds==300:return')
    new = new.replace('_wait(x)', '_wait(x, target_kind="first_trade_change_before_deadline")')
    assert _reference_module_compatible('first_change.py', old.encode(), new.encode())
    assert not _reference_module_compatible('first_change.py', old.encode(), new.replace('(1,5,30,60)', '(1,5,30,60,600)').encode())
    assert not _reference_module_compatible('trade_first_change.py', old.encode(), new.encode())
    assert not _reference_module_compatible('first_change.py', old.encode(), new.replace('seconds==300', 'seconds>=300').encode())
