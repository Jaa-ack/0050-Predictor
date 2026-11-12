from dataclasses import replace

import numpy as np
import pytest


def test_paper_limits_pass_at_exact_boundary_and_explain_each_block():
    from twquant.skill_risk import RiskLimits, check_target_weights
    limits = RiskLimits(max_weight=.2, max_exposure=.4, min_cash=.6,
                        max_single_volatility=.04, max_portfolio_volatility=.016,
                        max_capital_at_risk=.4)
    a = check_target_weights({'2330': .2, '2454': .2}, {'2330': .04, '2454': .04}, limits)
    assert a['verdict'] == 'PASS'
    assert a['cash_fraction'] == pytest.approx(.6)
    assert a['volatility_bound'] == pytest.approx(.016)
    for changed, code in [(dict(max_weight=.199), 'single_weight'),
                          (dict(max_exposure=.399), 'gross_exposure'),
                          (dict(min_cash=.601), 'cash_buffer'),
                          (dict(max_capital_at_risk=.399), 'capital_at_risk'),
                          (dict(max_single_volatility=.039), 'single_volatility'),
                          (dict(max_portfolio_volatility=.015), 'portfolio_volatility')]:
        r = check_target_weights({'2330': .2, '2454': .2}, {'2330': .04, '2454': .04}, replace(limits, **changed))
        assert r['verdict'] == 'BLOCK' and code in r['reasons']


@pytest.mark.parametrize('value', [np.nan, np.inf, -np.inf, True, -1, '0.2', None])
def test_nonfinite_or_invalid_targets_and_volatility_fail_closed(value):
    from twquant.skill_risk import RiskLimits, check_target_weights
    for weights, vols in [({'2330': value}, {'2330': .01}), ({'2330': .1}, {'2330': value})]:
        r = check_target_weights(weights, vols, RiskLimits())
        assert r['verdict'] == 'BLOCK' and 'invalid_or_missing_input' in r['reasons']


def test_missing_volatility_is_blocked_and_cash_only_does_not_need_it():
    from twquant.skill_risk import RiskLimits, check_target_weights
    assert check_target_weights({'2330': .1}, {}, RiskLimits())['verdict'] == 'BLOCK'
    cash = check_target_weights({}, {}, RiskLimits())
    assert cash['verdict'] == 'PASS' and cash['cash_fraction'] == 1
    assert check_target_weights({'2330': 0.}, {}, RiskLimits())['verdict'] == 'PASS'
    assert check_target_weights({'IX0001': .1}, {'IX0001': .01}, RiskLimits())['verdict'] == 'BLOCK'
    assert check_target_weights({'0050': .1}, {'0050': .01}, RiskLimits())['verdict'] == 'PASS'


@pytest.mark.parametrize('field,value', [('max_weight', 0), ('max_exposure', 1.1),
    ('min_cash', -.1), ('max_single_volatility', np.inf), ('max_portfolio_volatility', True),
    ('max_capital_at_risk', -.01)])
def test_invalid_limits_rejected(field, value):
    from twquant.skill_risk import RiskLimits
    with pytest.raises(ValueError):
        RiskLimits(**{field: value})


@pytest.mark.parametrize('ticker', ['TAIEX', '^TWII', 'UNKNOWN'])
def test_unknown_assets_and_index_aliases_cannot_be_assumed_tradable(ticker):
    from twquant.skill_risk import check_target_weights
    assert check_target_weights({ticker: .1}, {ticker: .01})['verdict'] == 'BLOCK'
