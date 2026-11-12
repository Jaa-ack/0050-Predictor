from math import comb

import numpy as np
import pytest


def test_gaussian_information_budget_has_exact_half_variance_threshold():
    from twquant.direction_feasibility import gaussian_sign_accuracy, required_gaussian_r2
    assert gaussian_sign_accuracy(0.) == .5
    assert gaussian_sign_accuracy(.5) == pytest.approx(.75)
    assert gaussian_sign_accuracy(1.) == 1.
    assert required_gaussian_r2(.75) == pytest.approx(.5)
    assert required_gaussian_r2(.5) == 0.
    assert required_gaussian_r2(1.) == 1.


@pytest.mark.parametrize('value', [-.1, 1.1, np.nan, np.inf, True])
def test_gaussian_information_budget_rejects_invalid_quality(value):
    from twquant.direction_feasibility import gaussian_sign_accuracy
    with pytest.raises(ValueError):
        gaussian_sign_accuracy(value)


def test_correlated_majority_does_not_assume_independent_model_errors():
    from twquant.direction_feasibility import majority_vote_accuracy
    expected = sum(comb(15, k) * .6**k * .4**(15-k) for k in range(8, 16))
    assert majority_vote_accuracy(.6, 15, 0.) == pytest.approx(expected)
    assert majority_vote_accuracy(.6, 15, 1.) == pytest.approx(.6)
    assert .6 < majority_vote_accuracy(.6, 15, .15) < expected
    assert majority_vote_accuracy(.6, 1, .3) == pytest.approx(.6)


def planning_config(**scenario_changes):
    scenario = dict(name='conditional_design', baseline_r2=.01, signal_r2=.6,
                    signal_availability=.98, unknown_answer_fraction=.001,
                    cross_stock_correlation=.35)
    scenario.update(scenario_changes)
    return dict(constituents=50, horizon=1, target_accuracy=.75, sessions=2000,
                seeds=[17, 29], scenarios=[scenario])


def test_full_daily_expectation_counts_missing_feeds_and_unknown_answers():
    from twquant.direction_feasibility import assess_feasibility, gaussian_sign_accuracy
    config = planning_config(signal_availability=.2, unknown_answer_fraction=.1)
    result = assess_feasibility(config)
    row = result['scenarios'][0]
    expected = .9 * (.2 * gaussian_sign_accuracy(.6) + .8 * gaussian_sign_accuracy(.01))
    assert row['expected']['overall_accuracy'] == pytest.approx(expected)
    assert row['expected']['worst_five_accuracy'] == pytest.approx(expected)
    assert row['prediction_coverage'] == 1.
    assert row['expected_target_met_under_assumptions'] is False
    assert row['required_signal_accuracy'][0] > 1.
    assert row['required_signal_r2'][0] is None
    assert row['perfect_signal_accuracy_ceiling'][0] < .75
    assert result['empirical_target_met'] is False
    assert result['unconditional_75_expectation_supported'] is False
    assert result['evidence_kind'] == 'synthetic_assumptions'
    assert result['models_trained'] == 0
    assert len(row['simulations']) == 2
    assert all(x['predictions'] == 2000 * 50 for x in row['simulations'])


def test_every_constituent_and_the_exact_worst_five_are_kept():
    from twquant.direction_feasibility import assess_feasibility, gaussian_sign_accuracy
    qualities = [.25] * 5 + [.9] * 45
    result = assess_feasibility(planning_config(signal_r2=qualities, signal_availability=1., unknown_answer_fraction=0.))
    row = result['scenarios'][0]
    assert row['expected']['overall_accuracy'] > .75
    assert row['expected']['worst_five_accuracy'] == pytest.approx(gaussian_sign_accuracy(.25))
    assert row['expected_target_met_under_assumptions'] is False
    assert len(row['expected']['per_constituent_accuracy']) == 50
    assert row['five_stock_combinations_covered'] == comb(50, 5)


def test_strong_assumptions_can_pass_only_the_conditional_flag_and_all_seeds_are_retained():
    from twquant.direction_feasibility import assess_feasibility
    config = planning_config(signal_r2=.7)
    a = assess_feasibility(config)
    b = assess_feasibility(config)
    assert a == b
    row = a['scenarios'][0]
    assert row['expected_target_met_under_assumptions'] is True
    assert [x['seed'] for x in row['simulations']] == [17, 29]
    assert abs(np.mean([x['overall_accuracy'] for x in row['simulations']]) - row['expected']['overall_accuracy']) < .015
    assert a['empirical_target_met'] is a['unconditional_75_expectation_supported'] is False
    assert row['required_signal_r2'][0] > .5


@pytest.mark.parametrize('change', [dict(constituents=5), dict(seeds=[17, 17]), dict(horizon=0),
                                  dict(sessions=True), dict(empirical_target_met=True)])
def test_planning_cannot_change_scope_hide_seeds_or_override_evidence(change):
    from twquant.direction_feasibility import assess_feasibility
    config = planning_config(); config.update(change)
    with pytest.raises(ValueError):
        assess_feasibility(config)


@pytest.mark.parametrize('change', [dict(signal_r2=[.6]*5), dict(signal_r2=.001),
                                  dict(signal_availability=np.nan), dict(unknown_answer_fraction=1.1)])
def test_planning_validates_quality_and_complete_stock_shapes(change):
    from twquant.direction_feasibility import assess_feasibility
    with pytest.raises(ValueError):
        assess_feasibility(planning_config(**change))


def test_no_new_signal_or_no_observable_answers_remains_explicitly_infeasible():
    from twquant.direction_feasibility import assess_feasibility
    for changes in [dict(signal_availability=0.), dict(unknown_answer_fraction=1.)]:
        row = assess_feasibility(planning_config(**changes))['scenarios'][0]
        assert row['expected_target_met_under_assumptions'] is False
        assert row['required_signal_accuracy'] == [None] * 50
        assert row['prediction_coverage'] == 1.


def test_a_sufficient_fallback_is_distinguished_from_an_impossible_missing_signal():
    from twquant.direction_feasibility import assess_feasibility
    row = assess_feasibility(planning_config(baseline_r2=1., signal_r2=1.,
        signal_availability=0., unknown_answer_fraction=0.))['scenarios'][0]
    assert row['expected_target_met_under_assumptions'] is True
    assert row['requirement_status'] == ['fallback_already_sufficient'] * 50
    assert row['minimum_signal_availability_if_perfect'] == [0.] * 50
