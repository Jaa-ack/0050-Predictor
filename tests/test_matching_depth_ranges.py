import json

import numpy as np
import pandas as pd
import pytest

from twquant.matching_simulation import MatchingScenario, _history, simulate_matching_panel
from twquant.queue_first_study import run_matching_study
from twquant.universe import TRAINING_TICKERS


def test_declared_constant_initial_depth_preserves_untouched_levels_and_passive_volume():
    days = pd.bdate_range('2020-01-02', periods=4)
    scenario = MatchingScenario('constant_depth', initial_depth_range=[13, 13],
                                market_fraction=0., cancel_fraction=0.)
    assert scenario.initial_depth_range == (13, 13)
    book, last, _, _, _, _ = _history(days, '2330', scenario, 17, np.ones(4))
    assert book.complete.all()
    np.testing.assert_array_equal(np.abs(book.quantities[:, :50]), 13)
    np.testing.assert_array_equal(np.abs(book.quantities[:, 78:]), 13)
    assert (np.abs(book.quantities).sum(axis=1) > 128 * 13).all()
    assert np.isin(last, [9999., 10000.]).all()


@pytest.mark.parametrize('depth', [(0, 10), (10, 5), (1, 1.5), (True, 10),
                                   (1, 1_000_001), (1, 2, 3)])
def test_depth_range_requires_two_positive_bounded_integers(depth):
    with pytest.raises(ValueError, match='[Dd]epth'):
        MatchingScenario('invalid', initial_depth_range=depth)


def test_external_depth_override_changes_only_that_stock_and_cannot_fit_primary_or_index():
    days = pd.bdate_range('2020-01-02', periods=8)
    universe = ('2330', '1216', '2002', 'IX0001')
    base = MatchingScenario('base', initial_depth_range=(30, 50),
                            market_fraction=.15, cancel_fraction=.35)
    changed = MatchingScenario('changed', initial_depth_range=(30, 50),
                               market_fraction=.15, cancel_fraction=.35,
                               external_depth_ranges=[['1216', [1, 2]]])
    a = simulate_matching_panel(universe, days, base, seed=17, future_seed=29)
    b = simulate_matching_panel(universe, days, changed, seed=17, future_seed=29)
    mask = a.ticker.ne('1216')
    pd.testing.assert_frame_equal(a[mask].reset_index(drop=True), b[mask].reset_index(drop=True))
    assert not a[~mask].reset_index(drop=True).equals(b[~mask].reset_index(drop=True))
    for ticker in ('2330', 'IX0001'):
        with pytest.raises(ValueError, match='[Ee]xternal'):
            MatchingScenario('invalid', external_depth_ranges=((ticker, (1, 2)),))
    with pytest.raises(ValueError, match='[Ee]xternal'):
        MatchingScenario('invalid', external_depth_ranges=(('1216', (1, 2)), ('1216', (3, 4))))


def test_custom_depth_retains_grid_and_future_seed_causality():
    days = pd.bdate_range('2020-01-02', periods=8)
    kwargs = dict(initial_depth_range=(50, 100), market_fraction=.15,
                  cancel_fraction=.35, queue={'flow_imbalance': .5})
    a = simulate_matching_panel(('2330', '1216'), days, MatchingScenario('a', grid_ticks=128, **kwargs), seed=17, future_seed=29)
    b = simulate_matching_panel(('2330', '1216'), days, MatchingScenario('b', grid_ticks=256, **kwargs), seed=17, future_seed=29)
    pd.testing.assert_frame_equal(a, b)
    c = simulate_matching_panel(('2330', '1216'), days, MatchingScenario('c', **kwargs), seed=17, future_seed=43)
    observed = [col for col in a if col.startswith(('f_book_', 'f_event_', 'f_trade_', 'observed_'))]
    pd.testing.assert_frame_equal(a[observed], c[observed])


def test_external_depth_study_rejects_nonconstituents_before_creating_output(tmp_path):
    members = list(TRAINING_TICKERS) + [str(4000 + i) for i in range(40)]
    membership = tmp_path / 'membership.json'
    membership.write_text(json.dumps(dict(tickers=members, count=50, as_of='2026-11-06',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',
        retrieved_at='2026-11-07T00:00:00+00:00', html_sha256='a' * 64)))
    config = dict(train_dates=30, calibration_dates=10, test_dates=126, seeds=[17],
                  max_wait_seconds=60, candidates=['prior'], scenarios=[dict(
                      name='outside', external_depth_ranges=[['1216', [1, 2]]])])
    output = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='constituents'):
        run_matching_study(config, membership, output)
    assert not output.exists()
