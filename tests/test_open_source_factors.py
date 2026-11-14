import numpy as np
import pandas as pd
import pytest

from twquant.demo import make_demo_prices
from twquant.features import build_features, observed_market_sessions


def test_alpha_subset_matches_hand_computed_linear_trend_and_candles():
    from twquant.open_source_factors import add_alpha_subset, ALPHA_COLUMNS
    prices = make_demo_prices(35)
    mask = prices.ticker.eq('2330')
    close = 100 + 2 * np.arange(mask.sum())
    prices.loc[mask, ['close', 'adj_close']] = np.column_stack([close, close])
    prices.loc[mask, 'open'] = close - 1
    prices.loc[mask, 'high'] = close + 2
    prices.loc[mask, 'low'] = close - 3
    frame = build_features(prices)
    result = add_alpha_subset(frame, observed_market_sessions(prices))
    row = result[result.ticker.eq('2330')].iloc[-1]
    assert row.f_oss_body_range == pytest.approx(.2)
    assert row.f_oss_upper_wick == pytest.approx(.4)
    assert row.f_oss_lower_wick == pytest.approx(.4)
    assert row.f_oss_slope20 == pytest.approx(2 / close[-1])
    assert row.f_oss_rsqr20 == pytest.approx(1)
    assert row.f_oss_resi20 == pytest.approx(0, abs=1e-12)
    assert row.f_oss_sump20 == pytest.approx(1)
    assert set(result) - set(frame) == set(ALPHA_COLUMNS)
    pd.testing.assert_frame_equal(frame, result[frame.columns])


def test_alpha_windows_keep_missing_sessions_and_ignore_labels_and_external_peers():
    from twquant.open_source_factors import add_alpha_subset, ALPHA_COLUMNS
    prices = make_demo_prices(50)
    calendar = observed_market_sessions(prices)
    prices = prices[~(prices.ticker.eq('2330') & prices.date.eq(calendar[-5]))]
    frame = build_features(prices)
    result = add_alpha_subset(frame, calendar)
    assert pd.isna(result[result.ticker.eq('2330')].iloc[-1].f_oss_slope20)
    changed = frame.copy()
    changed['target_1'] = .9
    changed['next_open'] = 9999
    pd.testing.assert_frame_equal(result[list(ALPHA_COLUMNS)],
        add_alpha_subset(changed, calendar)[list(ALPHA_COLUMNS)])


def test_prefix_audit_detects_future_features_and_checks_missing_values():
    from twquant.open_source_factors import causal_feature_audit
    from twquant.open_source_study import research_features
    prices = make_demo_prices(65)
    cutoffs = [sorted(prices.date.unique())[40]]
    good = causal_feature_audit(prices, research_features, cutoffs)
    assert good['passed'] and good['comparisons'] > 0

    def leaky(data):
        frame = research_features(data)
        frame['f_future'] = frame.groupby('ticker').adj_close.shift(-1)
        return frame

    bad = causal_feature_audit(prices, leaky, cutoffs)
    assert not bad['passed']
    assert 'f_future' in bad['changed_features']
