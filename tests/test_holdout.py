import numpy as np
import pandas as pd
import pytest


def holdout_frame(sessions=130):
    rng = np.random.default_rng(43)
    dates = pd.bdate_range("2024-01-02", periods=sessions)
    benchmark = 100 * np.exp(np.cumsum(rng.normal(.0001, .006, sessions)))
    frames = []
    for ticker in ["0050", "2330", "2454", "2881"]:
        signal = rng.normal(size=sessions)
        close = benchmark if ticker == "0050" else 100 * np.exp(np.cumsum(rng.normal(0, .009, sessions)))
        out = pd.DataFrame({"ticker": ticker, "date": dates, "f_signal": signal,
                            "decision_ts": (dates.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).tz_convert("UTC"),
                            "close": close, "adj_close": close,
                            "next_open": pd.Series(close).shift(-1).to_numpy(),
                            "next_open_date": pd.Series(dates).shift(-1).to_numpy(),
                            "benchmark_adjusted_next_open": pd.Series(benchmark).shift(-1).to_numpy()})
        for h in [1, 5]:
            end = pd.Series(close).shift(-h)
            b_end = pd.Series(benchmark).shift(-h)
            out[f"target_{h}"] = np.log(end / close).to_numpy()
            out[f"label_end_{h}"] = pd.Series(dates).shift(-h).to_numpy()
            out[f"label_available_at_{h}"] = (
                out[f"label_end_{h}"].dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)
            ).dt.tz_convert("UTC")
            out[f"benchmark_target_{h}"] = np.log(b_end / benchmark).to_numpy()
            out[f"benchmark_trade_return_{h}"] = (b_end / pd.Series(benchmark).shift(-1) - 1).to_numpy()
            out[f"benchmark_end_adj_close_{h}"] = b_end.to_numpy()
            out[f"trade_return_{h}"] = (end / pd.Series(close).shift(-1) - 1).to_numpy()
            out[f"trade_valid_{h}"] = out[f"trade_return_{h}"].notna()
        frames.append(out)
    return pd.concat(frames, ignore_index=True)


def config():
    from twquant.research import ResearchConfig
    return ResearchConfig(horizons=(1, 5), min_train_dates=30, calibration_dates=10,
                          test_dates=10, max_folds=2)


def test_holdout_labels_cannot_change_frozen_models_or_predictions():
    from twquant.holdout import evaluate_holdout
    frame = holdout_frame()
    changed = frame.copy()
    first = sorted(frame.date.unique())[-15]
    changed.loc[changed.date >= first, ["target_1", "target_5"]] = .8
    a = evaluate_holdout(frame, ["f_signal"], config(), holdout_dates=15)
    b = evaluate_holdout(changed, ["f_signal"], config(), holdout_dates=15)
    assert set(a.predictions.ticker) == {"2330", "2454", "2881"}
    assert len(a.predictions) == 3 * 15 * 2
    np.testing.assert_allclose(a.predictions.expected_return, b.predictions.expected_return)
    np.testing.assert_allclose(a.predictions.probability_up, b.predictions.probability_up)
    assert {h: m.model_version for h, m in a.models.items()} == {h: m.model_version for h, m in b.models.items()}
    assert a.metrics["by_horizon"]["1"]["mae"] != b.metrics["by_horizon"]["1"]["mae"]


def test_training_excludes_labels_unavailable_at_freeze():
    from twquant.holdout import evaluate_holdout
    frame = holdout_frame()
    first = sorted(frame.date.unique())[-15]
    delayed = frame[(frame.ticker != "0050") & (frame.date < first)].tail(6).index
    frame.loc[delayed, "label_available_at_1"] = pd.Timestamp("2030-01-01T00:00:00Z")
    changed = frame.copy()
    changed.loc[delayed, "target_1"] = .9
    a = evaluate_holdout(frame, ["f_signal"], config(), holdout_dates=15)
    b = evaluate_holdout(changed, ["f_signal"], config(), holdout_dates=15)
    assert a.models[1].model_version == b.models[1].model_version
    assert pd.Timestamp(a.cutoff) < frame.loc[frame.date == first, "decision_ts"].min()
    assert all(pd.Timestamp(m.training_cutoff) == pd.Timestamp(a.cutoff) for m in a.models.values())


def test_missing_holdout_actuals_are_unscored_without_dropping_predictions():
    from twquant.holdout import evaluate_holdout
    frame = holdout_frame()
    first = sorted(frame.date.unique())[-15]
    frame.loc[frame.date >= first, ["target_1", "target_5"]] = np.nan
    result = evaluate_holdout(frame, ["f_signal"], config(), holdout_dates=15)
    assert len(result.predictions) == 3 * 15 * 2
    assert result.predictions.actual_return.isna().all()
    assert result.metrics["by_horizon"]["1"]["observations"] == 0
    assert result.metrics["by_horizon"]["5"]["test_dates"] == 0
    assert result.metrics["feasibility"]["passed"] is False


def test_holdout_protocol_requires_external_seal_and_real_evidence_length():
    from twquant.holdout import evaluate_holdout
    result = evaluate_holdout(holdout_frame(), ["f_signal"], config(), holdout_dates=15)
    assert result.protocol["untouched_verified"] is False
    assert result.protocol["holdout_dates"] == 15
    assert result.protocol["development_exclusion_required"] is True
    assert "freeze" in result.protocol["caller_requirements"].lower()
    assert result.metrics["feasibility"]["checks"]["at_least_126_reserved_sessions"] is False
    assert result.folds.calibration_label_end.lt(pd.Timestamp(result.protocol["holdout_start"])).all()


def test_invalid_holdout_length_and_insufficient_development_history():
    from twquant.holdout import evaluate_holdout
    with pytest.raises(ValueError, match="holdout"):
        evaluate_holdout(holdout_frame(), ["f_signal"], config(), holdout_dates=0)
    with pytest.raises(ValueError, match="history|holdout|dates"):
        evaluate_holdout(holdout_frame(45), ["f_signal"], config(), holdout_dates=20)


def test_perfect_direction_baseline_cannot_be_beaten_by_undefined_skill():
    from twquant.holdout import evaluate_holdout
    from twquant.research import ResearchConfig
    frame = holdout_frame()
    frame["target_1"] = .01
    cfg = ResearchConfig(horizons=(1,), min_train_dates=30, calibration_dates=10, test_dates=10)
    result = evaluate_holdout(frame, ["f_signal"], cfg, holdout_dates=15)
    scores = result.metrics["by_horizon"]["1"]
    assert scores["baseline_brier"] == 0
    assert scores["brier"] > 0
    assert result.metrics["feasibility"]["checks"]["h1_brier_not_worse_than_baseline"] is False
