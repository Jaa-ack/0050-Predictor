import numpy as np
import pandas as pd
import pytest


def research_frame(sessions=150):
    rng = np.random.default_rng(9)
    dates = pd.bdate_range("2023-01-02", periods=sessions)
    frames = []
    benchmark = rng.normal(0.0002, 0.009, sessions)
    for ticker in ["2330", "2454", "2881"]:
        signal = rng.normal(0, 1, sessions)
        returns = rng.normal(0, 0.01, sessions) + 0.002 * np.roll(signal, 1)
        prices = 100 * np.exp(np.cumsum(returns))
        f = pd.DataFrame({
            "ticker": ticker, "date": dates,
            "decision_ts": (dates + pd.Timedelta(hours=20)).tz_localize("Asia/Taipei").tz_convert("UTC"),
            "close": prices, "adj_close": prices,
            "next_open": pd.Series(prices).shift(-1).to_numpy() * 0.999,
            "next_open_date": pd.Series(dates).shift(-1).to_numpy(),
            "f_signal": signal, "f_missing": np.nan,
        })
        for h in [1, 5, 20]:
            f[f"target_{h}"] = np.log(pd.Series(prices).shift(-h) / prices).to_numpy()
            f[f"label_end_{h}"] = pd.Series(dates).shift(-h).to_numpy()
            f[f"benchmark_target_{h}"] = pd.Series(benchmark).rolling(h).sum().shift(-h).to_numpy()
            f[f"trade_return_{h}"] = np.expm1(f[f"target_{h}"]) + 0.001
            f[f"benchmark_trade_return_{h}"] = np.expm1(f[f"benchmark_target_{h}"]) + 0.001
            f[f"trade_valid_{h}"] = f[f"target_{h}"].notna()
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


def small_config(**kwargs):
    from twquant.research import ResearchConfig
    return ResearchConfig(**{
        "horizons": (1, 5), "min_train_dates": 40,
        "calibration_dates": 15, "test_dates": 12, "max_folds": 3,
        **kwargs,
    })


def test_purge_uses_label_maturity_and_keeps_all_stocks_on_same_side():
    from twquant.research import temporal_splits
    frame = research_frame()
    folds = temporal_splits(frame, 5, small_config())
    assert len(folds) == 3
    for split in folds:
        train, calibration, test = (frame.loc[split[k]] for k in ["train", "calibration", "test"])
        assert train["label_end_5"].max() < calibration["date"].min()
        assert calibration["label_end_5"].max() < test["date"].min()
        assert not set(train.date) & set(calibration.date)
        assert not set(calibration.date) & set(test.date)
        assert test.groupby("date").ticker.nunique().eq(3).all()


def test_walk_forward_produces_calibrated_out_of_sample_distributions():
    from twquant.research import run_research
    frame = research_frame()
    result = run_research(frame, ["f_signal", "f_missing"], small_config())
    p = result.predictions
    assert set(p.horizon) == {1, 5}
    assert p.actual_return.notna().all()
    assert p.probability_up.between(0, 1).all()
    assert (p.q10 <= p.q50).all() and (p.q50 <= p.q90).all()
    assert np.isfinite(p[["expected_return", "q10", "q90"]]).all().all()
    assert "brier" in result.metrics["by_horizon"]["1"]
    assert "strategy_net" in result.equity
    assert result.folds["train_label_end"].lt(result.folds["calibration_start"]).all()
    assert result.folds["calibration_label_end"].lt(result.folds["test_start"]).all()


def test_test_labels_cannot_change_the_model_or_its_predictions():
    from twquant.research import run_research, temporal_splits
    frame = research_frame()
    cfg = small_config(horizons=(1,), max_folds=1)
    split = temporal_splits(frame, 1, cfg)[0]
    modified = frame.copy()
    modified.loc[split["test"], "target_1"] = 0.8
    a = run_research(frame, ["f_signal"], cfg)
    b = run_research(modified, ["f_signal"], cfg)
    np.testing.assert_allclose(a.predictions.probability_up, b.predictions.probability_up)
    np.testing.assert_allclose(a.predictions.expected_return, b.predictions.expected_return)
    assert a.models[1].model_version == b.models[1].model_version


def test_cash_to_cash_strategy_charges_both_sides_and_obeys_exposure_cap():
    from twquant.research import simulate_portfolio
    cfg = small_config(commission=0.01, sell_tax=0.02, slippage=0.005,
                       max_positions=5, max_weight=0.2)
    rows = pd.DataFrame({
        "ticker": ["2330", "2454"], "date": [pd.Timestamp("2024-01-01")] * 2,
        "next_open_date": [pd.Timestamp("2024-01-02")] * 2,
        "horizon": [1, 1], "probability_up": [0.9, 0.9],
        "expected_return": [0.2, 0.2], "trade_return_1": [0.1, 0.1],
        "benchmark_trade_return_1": [0.01, 0.01], "benchmark_target_1": [0.01, 0.01],
        "next_open": [100.0, 200.0], "trade_valid_1": [True, True],
    })
    equity, metrics = simulate_portfolio(rows, cfg)
    expected = 0.4 * (1.1 * (1 - 0.01 - 0.02 - 0.005) / (1 + 0.01 + 0.005) - 1)
    assert equity.iloc[0].strategy_net == pytest.approx(1 + expected)
    assert equity.iloc[0].exposure <= 0.4
    assert metrics["orders"] == 4
    rows["trade_valid_1"] = False
    cash, _ = simulate_portfolio(rows, cfg)
    assert cash.iloc[0].strategy_net == 1


def test_future_targets_are_rejected_as_features_and_short_history_is_explicit():
    from twquant.research import run_research
    with pytest.raises(ValueError, match="feature|target"):
        run_research(research_frame(), ["target_1"], small_config())
    with pytest.raises(ValueError, match="history|dates"):
        run_research(research_frame(20), ["f_signal"], small_config())


def test_latest_training_ignores_labels_that_have_not_matured():
    from twquant.research import train_latest
    frame = research_frame()
    cutoff = frame.date.sort_values().unique()[115]
    as_of = pd.Timestamp(cutoff).tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)
    changed = frame.copy()
    changed.loc[changed.date > cutoff, "target_1"] = 1.5
    cfg = small_config(horizons=(1,))
    a = train_latest(frame, ["f_signal"], cfg, as_of)
    b = train_latest(changed, ["f_signal"], cfg, as_of)
    assert a[1].model_version == b[1].model_version
    assert pd.Timestamp(a[1].calibration_ends).date() <= as_of.date()


def test_walk_forward_purges_outcomes_with_delayed_observed_availability():
    from twquant.research import temporal_splits
    frame = research_frame()
    frame["label_available_at_5"] = (
        (frame.label_end_5.dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20))
        .dt.tz_convert("UTC")
    )
    cfg = small_config(max_folds=1)
    first = temporal_splits(frame, 5, cfg)[0]
    late_index = first["train"][-1]
    frame.loc[late_index, "label_available_at_5"] = pd.Timestamp("2030-01-01T00:00:00Z")
    split = temporal_splits(frame, 5, cfg)[0]
    assert late_index not in split["train"]


def test_direction_target_is_not_a_feature_and_explanations_are_labeled_proxy():
    from twquant.research import run_research
    cfg = small_config(horizons=(1,), max_folds=1)
    result = run_research(research_frame(), ["f_signal"], cfg)
    explanation = result.models[1].explain(research_frame().head(1), top_n=1)
    assert explanation[0][0]["feature"] == "f_signal"
    assert explanation[0][0]["method"] == "standardized_linear_baseline_proxy"


def test_unfillable_selected_order_stays_cash_without_hindsight_replacement():
    from twquant.research import simulate_portfolio
    rows = pd.DataFrame({
        "ticker": ["2330", "2454"], "date": [pd.Timestamp("2024-01-01")] * 2,
        "next_open_date": [pd.Timestamp("2024-01-02")] * 2,
        "horizon": [1, 1], "probability_up": [.9, .8], "expected_return": [.2, .1],
        "trade_return_1": [np.nan, .1], "benchmark_trade_return_1": [.01, .01],
        "benchmark_target_1": [.01, .01], "next_open": [np.nan, 100.],
        "trade_valid_1": [False, True],
    })
    equity, metrics = simulate_portfolio(rows, small_config(max_positions=1, max_weight=1.))
    assert equity.iloc[0].strategy_net == 1.
    assert metrics["orders"] == 0


def test_return_predictor_does_not_refit_its_center_on_calibration_labels():
    from twquant.research import run_research
    result = run_research(research_frame(), ["f_signal"], small_config(horizons=(1,), max_folds=1))
    assert result.models[1].return_bias == 0.


def test_missing_future_stock_price_remains_in_decision_universe():
    from twquant.research import run_research, temporal_splits
    frame = research_frame()
    cfg = small_config(horizons=(1,), max_folds=1)
    index = temporal_splits(frame, 1, cfg)[0]["test"][0]
    ticker, date = frame.loc[index, ["ticker", "date"]]
    frame.loc[index, ["target_1", "trade_return_1"]] = np.nan
    frame.loc[index, "trade_valid_1"] = False
    result = run_research(frame, ["f_signal"], cfg)
    row = result.predictions.query("ticker == @ticker and date == @date")
    assert len(row) == 1
    assert pd.isna(row.iloc[0].actual_return)
    assert result.metrics["by_horizon"]["1"]["observations"] == len(result.predictions) - 1


def test_passive_benchmark_captures_returns_across_missing_evaluation_dates():
    from twquant.research import simulate_portfolio
    rows = pd.DataFrame({
        "ticker": ["2330", "2330"], "date": pd.to_datetime(["2024-01-01", "2024-01-04"]),
        "next_open_date": pd.to_datetime(["2024-01-02", "2024-01-05"]),
        "horizon": [1, 1], "probability_up": [.1, .1], "expected_return": [.01, .01],
        "trade_return_1": [.1, .1], "benchmark_trade_return_1": [.1, .1],
        "benchmark_target_1": np.log([1.1, 1.1]), "next_open": [100., 120.],
        "trade_valid_1": [True, True], "benchmark_adjusted_next_open": [100., 120.],
        "benchmark_end_adj_close_1": [110., 132.],
    })
    cfg = small_config(commission=0., slippage=0., sell_tax=0., etf_sell_tax=0.)
    equity, metrics = simulate_portfolio(rows, cfg)
    assert equity.iloc[-1].benchmark_buy_hold_net == pytest.approx(1.32)
    assert metrics["benchmark_buy_hold"]["total_return"] == pytest.approx(.32)


def test_benchmark_halt_keeps_stock_trades_but_withholds_passive_daily_risk():
    from twquant.research import simulate_portfolio
    rows=pd.DataFrame({'ticker':['2330']*3,'date':pd.to_datetime(['2025-06-09','2025-06-10','2025-06-11']),
        'next_open_date':pd.to_datetime(['2025-06-10','2025-06-11','2025-06-12']),
        'horizon':[1]*3,'probability_up':[.9]*3,'expected_return':[.1]*3,
        'trade_return_1':[.1]*3,'benchmark_trade_return_1':[.1,np.nan,.1],
        'benchmark_target_1':np.log([1.1,np.nan,1.1]),'next_open':[100.]*3,
        'trade_valid_1':[True]*3,'benchmark_adjusted_next_open':[100.,np.nan,120.],
        'benchmark_end_adj_close_1':[110.,np.nan,132.]})
    cfg=small_config(commission=0.,slippage=0.,sell_tax=0.,etf_sell_tax=0.)
    equity,metrics=simulate_portfolio(rows,cfg)
    assert len(equity)==3
    assert equity.iloc[1].strategy_return>0
    assert equity.iloc[1].benchmark_return==0
    assert pd.isna(equity.iloc[1].benchmark_buy_hold_net)
    assert metrics['benchmark_unexecutable_dates']==1
    assert metrics['benchmark_buy_hold']['total_return']==pytest.approx(.32)
    assert metrics['benchmark_buy_hold']['sharpe'] is None


def test_fold_limit_keeps_matured_blocks_instead_of_unobservable_terminal_dates():
    from twquant.research import run_research
    config = small_config(horizons=(20,), min_train_dates=30, calibration_dates=10,
                          test_dates=15, max_folds=1)
    result = run_research(research_frame(130), ["f_signal"], config)
    assert result.predictions.date.nunique() >= 1
    assert result.predictions.actual_return.notna().all()


def test_latest_training_cannot_use_a_label_first_received_during_calibration():
    from twquant.research import train_latest
    frame = research_frame()
    frame["label_available_at_1"] = (
        (frame.label_end_1.dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20))
        .dt.tz_convert("UTC")
    )
    dates = pd.DatetimeIndex(frame.date.unique()).sort_values()
    delayed = frame.index[(frame.ticker == "2330") & (frame.date == dates[20])][0]
    # This old outcome arrives after the calibration period starts, although it
    # is available by the final fitting cutoff. It cannot train the frozen model.
    frame.loc[delayed, "label_available_at_1"] = (
        dates[140].tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)
    ).tz_convert("UTC")
    excluded = frame.copy()
    excluded.loc[delayed, "target_1"] = np.nan
    config = small_config(horizons=(1,))
    cutoff = frame.decision_ts.max()
    actual = train_latest(frame, ["f_signal"], config, as_of=cutoff)[1]
    expected = train_latest(excluded, ["f_signal"], config, as_of=cutoff)[1]
    assert actual.model_version == expected.model_version
    np.testing.assert_allclose(actual.predict(frame.tail(3)).expected_return,
                               expected.predict(frame.tail(3)).expected_return)
