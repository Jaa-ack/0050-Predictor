"""Exercise persisted research, daily forecasts and maturity with real modules."""
import json

import numpy as np
import pandas as pd
import pytest

from twquant.data import PriceStore
from twquant.demo import make_demo_prices
from twquant.research import ResearchConfig
from twquant.workflows import current_report, forecast, ledger_for, research, settle


def integration_config():
    return ResearchConfig(min_train_dates=30, calibration_dates=10,
                          test_dates=15, max_folds=2)


def at_session(prices, index, hour=20, minute=0):
    date = pd.DatetimeIndex(prices.date.unique()).sort_values()[index]
    return date.tz_localize("Asia/Taipei") + pd.Timedelta(hours=hour, minutes=minute)


def test_persisted_research_forecast_idempotence_and_three_maturity_windows(tmp_path, monkeypatch):
    prices = make_demo_prices(150, seed=71)
    cutoff = at_session(prices, 129)
    initial = prices.loc[prices.available_at <= cutoff].copy()
    store = PriceStore(tmp_path / "data")
    store.write_prices(initial)
    report = research(tmp_path, integration_config(), as_of=cutoff, holdout_dates=20)
    assert report["data_kind"] == "synthetic"
    assert report["readiness"]["passed"] is False
    assert "synthetic_data" in report["readiness"]["blockers"]
    assert current_report(tmp_path)["run_id"] == report["run_id"]
    run_folder = tmp_path / "runs" / report["run_id"]
    assert (run_folder / "models.joblib").is_file()
    seal = json.loads((run_folder / "seal.json").read_text())
    development = pd.read_parquet(run_folder / "features.parquet")
    for horizon in (1, 5, 20):
        used = development.loc[development[f"target_{horizon}"].notna()]
        assert (pd.to_datetime(used[f"label_available_at_{horizon}"], utc=True)
                < pd.Timestamp(seal["first_holdout_decision"])).all()
    oos = pd.read_parquet(run_folder / "oos_predictions.parquet")
    assert oos.label_end_date.max() < pd.Timestamp(seal["holdout_start"])
    first = forecast(tmp_path, cutoff, paper=True)
    again = forecast(tmp_path, cutoff, paper=True)
    assert (first["inserted"], first["total"], again["inserted"]) == (30, 30, 0)
    assert len(ledger_for(tmp_path).forecasts()) == 30
    assert settle(tmp_path, cutoff)["newly_settled"] == 0

    store.write_prices(prices)
    assert settle(tmp_path, at_session(prices, 130))["newly_settled"] == 10
    assert settle(tmp_path, at_session(prices, 130))["newly_settled"] == 0
    assert settle(tmp_path, at_session(prices, 134))["newly_settled"] == 10
    final = settle(tmp_path, at_session(prices, 149))
    assert final["newly_settled"] == 10
    assert final["monitoring"]["settled"] == 30
    assert {h: values["count"] for h, values in final["monitoring"]["by_horizon"].items()} == {"1": 10, "5": 10, "20": 10}
    assert all(f["is_replay"] for f in ledger_for(tmp_path).forecasts())
    assert ledger_for(tmp_path).monitoring(eligible_only=True)["settled"] == 0
    import twquant.workflows as workflows
    monkeypatch.setattr(workflows, "source_digest", lambda: "changed_after_training")
    with pytest.raises(ValueError, match="Code changed"):
        forecast(tmp_path, cutoff, paper=True)


def test_preopen_inference_uses_prior_completed_prices_with_preopen_model(tmp_path):
    prices = make_demo_prices(150, seed=88)
    # Full data exists, but both training and inference must respect their cutoff.
    PriceStore(tmp_path / "data").write_prices(prices)
    fit_cutoff = at_session(prices, 129)
    report = research(tmp_path, integration_config(), as_of=fit_cutoff,
                      mode="preopen", holdout_dates=20)
    decision = at_session(prices, 130, hour=8, minute=30)
    result = forecast(tmp_path, decision, paper=True)
    assert result["total"] == 30
    assert report["provenance"]["mode"] == "preopen"
    for row in result["forecasts"]:
        assert pd.Timestamp(row["anchor_date"]).date() == fit_cutoff.date()
        assert pd.Timestamp(row["decision_ts"]) == decision
        assert 0 <= row["probability_up"] <= 1
        assert row["q10"] <= row["q50"] <= row["q90"]
        assert np.isfinite([row["expected_return"], row["price_low"], row["price_high"]]).all()
        assert row["mode"] == "preopen"
    with pytest.raises(ValueError, match="Preopen"):
        forecast(tmp_path, at_session(prices, 130), paper=True)
