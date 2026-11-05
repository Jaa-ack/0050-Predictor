import numpy as np
import pandas as pd
import pytest

from twquant.features import build_features, get_feature_columns, point_in_time_join


def prices_fixture(n=100):
    dates = pd.bdate_range("2024-01-02", periods=n)
    rows = []
    for j, ticker in enumerate(["0050", "2330", "2881"]):
        for i, date in enumerate(dates):
            close = 100.0 + j * 20 + i * 0.5 + np.sin(i) * 0.2
            rows.append({"ticker": ticker, "date": date, "open": close - 0.1,
                         "high": close + 1, "low": close - 1, "close": close,
                         "adj_close": close, "volume": 1_000_000 + i * 10,
                         "available_at": date.tz_localize("Asia/Taipei") + pd.Timedelta(hours=18),
                         "source": "fixture", "adjustment_source": "fixture"})
    return pd.DataFrame(rows)


def test_features_use_only_past_prices_and_not_target_columns():
    prices = prices_fixture()
    before = build_features(prices)
    changed = prices.copy()
    changed.loc[changed.date >= pd.Timestamp("2024-04-01"),
                ["open", "high", "low", "close", "adj_close"]] *= 2
    after = build_features(changed)
    cols = get_feature_columns(before)
    assert cols and all(c.startswith("f_") for c in cols)
    assert not set(cols).intersection({"target_1", "target_5", "target_20", "next_open"})
    mask = before.date < pd.Timestamp("2024-04-01")
    pd.testing.assert_frame_equal(before.loc[mask, cols], after.loc[mask, cols])


def test_targets_and_execution_returns_have_different_entry_prices():
    prices = prices_fixture()
    frame = build_features(prices)
    rows = prices.loc[prices.ticker == "2330"].reset_index(drop=True)
    row = frame.loc[frame.ticker == "2330"].iloc[25]
    assert row.target_5 == pytest.approx(np.log(rows.adj_close.iloc[30] / rows.adj_close.iloc[25]))
    assert row.label_end_5 == rows.date.iloc[30]
    assert row.label_available_at_5 == rows.available_at.iloc[30].tz_convert("UTC")
    assert row.trade_return_5 == pytest.approx(rows.close.iloc[30] / rows.open.iloc[26] - 1)
    assert row.next_open_date == rows.date.iloc[26]
    benchmark = prices.loc[prices.ticker == "0050"].reset_index(drop=True)
    assert row.benchmark_adj_close == benchmark.adj_close.iloc[25]
    assert row.benchmark_adjusted_next_open == benchmark.open.iloc[26]
    assert row.benchmark_end_adj_close_5 == benchmark.adj_close.iloc[30]
    assert pd.isna(frame.loc[frame.ticker == "2330"].iloc[-1].target_1)


def test_split_adjustment_keeps_technical_returns_and_gap_neutral():
    prices = prices_fixture()
    split_date = sorted(prices.date.unique())[50]
    split = prices.copy()
    split.loc[split.date >= split_date, ["open", "high", "low", "close"]] /= 4
    before = build_features(prices)
    after = build_features(split)
    cols = ["f_return_1", "f_gap", "f_intraday", "f_range"]
    pd.testing.assert_frame_equal(before[cols], after[cols])
    np.testing.assert_allclose(before.trade_return_1, after.trade_return_1, equal_nan=True)


def test_late_price_is_excluded_until_it_was_available():
    prices = prices_fixture()
    date = pd.Timestamp("2024-03-04")
    prices.loc[(prices.ticker == "2330") & (prices.date == date), "available_at"] = date.tz_localize("Asia/Taipei") + pd.Timedelta(days=2)
    frame = build_features(prices)
    assert frame.loc[(frame.ticker == "2330") & (frame.date == date)].empty


def test_missing_session_marks_execution_unavailable_instead_of_skipping_suspend():
    prices = prices_fixture()
    missing = sorted(prices.date.unique())[26]
    prices = prices.loc[~((prices.ticker == "2330") & (prices.date == missing))]
    frame = build_features(prices)
    prior = frame.loc[frame.ticker == "2330"].iloc[25]
    assert not prior.trade_valid_1
    assert pd.isna(prior.trade_return_1)


def test_etf_suspension_does_not_remove_stock_sessions_or_fabricate_benchmark_prices():
    prices=prices_fixture()
    dates=sorted(prices.date.unique())
    prices=prices[~((prices.ticker=='0050') & prices.date.isin(dates[26:31]))]
    frame=build_features(prices)
    stock=frame[frame.ticker=='2330'].set_index('date')
    assert stock.loc[dates[25],'label_end_1']==dates[26]
    assert stock.loc[dates[25],'label_end_5']==dates[30]
    assert stock.loc[dates[25],'trade_valid_1']
    # Core-ten market context remains available when only the test ETF halts.
    assert np.isfinite(stock.loc[dates[26],'f_market_return_1'])
    assert pd.isna(stock.loc[dates[26],'benchmark_adj_close'])
    preopen=build_features(prices,mode='preopen')
    decision=preopen[(preopen.ticker=='2330') & (preopen.date==dates[25])].iloc[0]
    assert decision.decision_ts.tz_convert('Asia/Taipei').date()==pd.Timestamp(dates[26]).date()


def test_daily_flat_range_is_not_used_as_future_information_to_select_opening_orders():
    prices = prices_fixture()
    dates = sorted(prices.date.unique())
    mask = (prices.ticker == "2330") & (prices.date == dates[26])
    prices.loc[mask, ["high", "low"]] = prices.loc[mask, "open"].iloc[0]
    prices.loc[mask, "close"] = prices.loc[mask, "open"].iloc[0]
    prices.loc[mask, "adj_close"] = prices.loc[mask, "open"].iloc[0]
    frame = build_features(prices)
    prior = frame.loc[(frame.ticker == "2330") & (frame.date == dates[25])].iloc[0]
    assert prior.trade_valid_1


def test_pit_join_uses_publication_availability_and_revision_timestamps():
    decisions = pd.DataFrame({"ticker": ["2330"] * 3,
                              "decision_ts": pd.to_datetime(["2024-04-01T12:00Z", "2024-04-03T12:00Z", "2024-04-05T12:00Z"])})
    events = pd.DataFrame({"ticker": ["2330", "2330"], "feature": ["eps", "eps"],
                           "value": [3.0, 2.5], "period_end": ["2024-03-31"] * 2,
                           "published_at": ["2024-04-02T01:00Z", "2024-04-04T01:00Z"],
                           "available_at": ["2024-04-02T02:00Z", "2024-04-04T02:00Z"],
                           "version": [1, 2]})
    result = point_in_time_join(decisions, events)
    assert pd.isna(result.f_ext_eps.iloc[0])
    assert result.f_ext_eps.tolist()[1:] == [3.0, 2.5]
    assert result.f_ext_eps_age_days.iloc[2] == pytest.approx(1 + 10 / 24)
    assert len(events) == 2


def test_pit_join_rejects_available_before_publication_and_unsafe_feature_names():
    decisions = pd.DataFrame({"ticker": ["2330"], "decision_ts": pd.to_datetime(["2024-04-03T12:00Z"])})
    events = pd.DataFrame({"ticker": ["2330"], "feature": ["eps"], "value": [3],
                           "published_at": ["2024-04-02T02:00Z"], "available_at": ["2024-04-02T01:00Z"]})
    with pytest.raises(ValueError, match="published"):
        point_in_time_join(decisions, events)
    events["available_at"] = "2024-04-02T03:00Z"
    events["feature"] = "target_1; drop table"
    with pytest.raises(ValueError, match="feature"):
        point_in_time_join(decisions, events)


def test_historical_preopen_uses_next_actual_benchmark_session_and_no_guessed_terminal_session():
    prices = prices_fixture()
    frame = build_features(prices, mode="preopen")
    first = frame.loc[frame.ticker == "2330"].iloc[0]
    sessions = sorted(prices.date.unique())
    expected = (pd.Timestamp(sessions[1]).tz_localize("Asia/Taipei") + pd.Timedelta(hours=8, minutes=30)).tz_convert("UTC")
    assert first.decision_ts == expected
    assert first.date == sessions[0]
    assert pd.isna(frame.loc[frame.ticker == "2330"].iloc[-1].decision_ts)


def test_preopen_external_feature_can_use_overnight_information_but_never_same_day_close():
    prices = prices_fixture()
    sessions = sorted(prices.date.unique())
    date = pd.Timestamp(sessions[20])
    next_date = pd.Timestamp(sessions[21])
    events = pd.DataFrame({"ticker": ["2330"], "feature": ["overnight"], "value": [1.0],
                           "published_at": [next_date.tz_localize("Asia/Taipei") + pd.Timedelta(hours=6)],
                           "available_at": [next_date.tz_localize("Asia/Taipei") + pd.Timedelta(hours=7)]})
    frame = build_features(prices, external=events, mode="preopen")
    row = frame.loc[(frame.ticker == "2330") & (frame.date == date)].iloc[0]
    post = build_features(prices)
    reference = post.loc[(post.ticker == "2330") & (post.date == date)].iloc[0]
    assert row.f_ext_overnight == 1.0
    assert row.f_return_1 == reference.f_return_1
    assert pd.isna(frame.loc[frame.ticker == "2330"].iloc[-1].f_ext_overnight)
