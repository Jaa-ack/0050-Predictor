from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from test_research import small_config
from twquant.research import simulate_portfolio


def rows():
    return pd.DataFrame(dict(ticker=['2330', '2454'], date=pd.to_datetime(['2024-01-01'] * 2),
        next_open_date=pd.to_datetime(['2024-01-02'] * 2), horizon=1,
        probability_up=[.9, .8], expected_return=[.2, .1],
        trade_return_1=[.1, -.1], benchmark_trade_return_1=.01, benchmark_target_1=.01,
        next_open=100., trade_valid_1=True))


def plan(p, weights=(.2, .2), eligible=(True, True)):
    return p[['date', 'ticker']].assign(weight=list(weights), eligible=list(eligible))


def test_disabled_overlay_is_identical_and_filter_keeps_daily_denominator():
    p = rows()
    cfg = small_config()
    original, metric = simulate_portfolio(p, cfg)
    explicit, explicit_metric = simulate_portfolio(p, cfg, decisions=plan(p))
    pd.testing.assert_frame_equal(original, explicit)
    assert metric == explicit_metric
    filtered, filtered_metric = simulate_portfolio(p, cfg, decisions=plan(p, eligible=(False, False)))
    assert len(filtered) == len(original)
    assert filtered.strategy_net.eq(1.).all() and filtered_metric['orders'] == 0
    pd.testing.assert_frame_equal(p, rows())


def test_weights_include_entry_cost_and_unfilled_capital_stays_cash_without_replacement():
    p = rows()
    cfg = small_config(commission=.01, slippage=0, sell_tax=0, max_positions=1, max_weight=1)
    one, metrics = simulate_portfolio(p, cfg, decisions=plan(p, weights=(.4, .3)))
    assert one.exposure.iloc[0] == pytest.approx(.4 / 1.01)
    assert metrics['strategy']['total_return'] == pytest.approx(.4 * (1.1 * .99 / 1.01 - 1))
    p.loc[0, 'trade_valid_1'] = False
    unfilled, metric = simulate_portfolio(p, cfg, decisions=plan(p, weights=(.4, .3)))
    assert metric['orders'] == 0 and unfilled.strategy_net.iloc[0] == 1


def test_rule_strategy_selection_is_independent_of_model_scores_and_future_returns():
    p = rows()
    cfg = small_config(max_positions=1, max_weight=1)
    decisions = plan(p, weights=(.5, .5)).assign(score=[0., 1.])
    a, _ = simulate_portfolio(p, cfg, decisions=decisions, rule_only=True)
    changed = p.copy()
    changed['probability_up'] = 0
    changed['expected_return'] = [-.8, -.9]
    changed.loc[0, 'trade_return_1'] = 10
    b, _ = simulate_portfolio(changed, cfg, decisions=decisions, rule_only=True)
    pd.testing.assert_frame_equal(a[['exposure', 'positions', 'strategy_return']],
                                  b[['exposure', 'positions', 'strategy_return']])
    assert a.strategy_gross_return.iloc[0] == pytest.approx(-.05)


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'nonfinite', 'negative', 'cap', 'bool', 'future_column'])
def test_invalid_decision_overlay_refused(kind):
    p = rows()
    d = plan(p)
    if kind == 'missing': d = d.iloc[:1]
    if kind == 'duplicate': d = pd.concat([d, d.iloc[[0]]])
    if kind == 'nonfinite': d.loc[0, 'weight'] = np.nan
    if kind == 'negative': d.loc[0, 'weight'] = -.1
    if kind == 'cap': d.loc[0, 'weight'] = .3
    if kind == 'bool': d['eligible'] = 'yes'
    if kind == 'future_column': d['trade_return_1'] = .1
    with pytest.raises(ValueError):
        simulate_portfolio(p, small_config(), decisions=d)
