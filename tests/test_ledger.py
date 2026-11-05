import pandas as pd
import pytest

from twquant.ledger import ForecastLedger


def forecast(**changes):
    record = dict(ticker="2330", anchor_date="2025-01-02", decision_ts="2025-01-02T12:00:00Z",
                  horizon=1, model_version="m1", snapshot_id="s1", probability_up=0.7,
                  expected_return=0.01, q10=-0.02, q50=0.01, q90=0.04,
                  reference_close=100.0, reference_adj_close=50.0,
                  status="paper", features={"f_return_1":0.01}, data_cutoff="2025-01-02T12:00:00Z")
    record.update(changes)
    return record


def prices():
    return pd.DataFrame([
        {"ticker":t,"date":pd.Timestamp(d),"adj_close":p,"available_at":pd.Timestamp(d+"T12:00:00Z")}
        for t in ["0050","2330"]
        for d,p in [("2025-01-02",25.0),("2025-01-03",25.5),("2025-01-06",26.0)]
    ])


def test_forecasts_immutable_idempotent_and_mature_only_when_available(tmp_path):
    ledger=ForecastLedger(tmp_path/"ledger.sqlite")
    assert ledger.append([forecast()]) == 1
    assert ledger.append([forecast()]) == 0
    assert ledger.settle(prices(),"2025-01-03T08:30:00+08:00","s2") == 0
    assert ledger.settle(prices(),"2025-01-03T20:00:00+08:00","s2") == 1
    assert ledger.settle(prices(),"2025-01-06T20:00:00+08:00","s3") == 0
    # New snapshot rebases all adjusted prices; ratio must use same vintage.
    m=ledger.monitoring()
    assert m["settled"] == 1
    assert m["by_horizon"]["1"]["mae"] == pytest.approx(0.01)
    assert ledger.forecasts()[0]["actual_return"] == pytest.approx(0.02)


def test_settlement_does_not_shift_suspended_ticker_target_date(tmp_path):
    ledger=ForecastLedger(tmp_path/"ledger.sqlite")
    ledger.append([forecast()])
    p=prices()
    p=p[~((p.ticker=="2330") & (p.date==pd.Timestamp("2025-01-03")))]
    assert ledger.settle(p,"2025-01-06T20:00:00+08:00","s2") == 0


def test_reject_timezone_naive_and_changed_existing_forecast(tmp_path):
    ledger=ForecastLedger(tmp_path/"ledger.sqlite")
    with pytest.raises(ValueError,match="timezone"):
        ledger.append([forecast(decision_ts="2025-01-02 20:00:00")])
    ledger.append([forecast()])
    with pytest.raises(ValueError,match="immutable"):
        ledger.append([forecast(probability_up=0.1)])


def test_reject_invalid_probability_and_interval(tmp_path):
    ledger=ForecastLedger(tmp_path/"ledger.sqlite")
    with pytest.raises(ValueError):
        ledger.append([forecast(probability_up=1.4)])
    with pytest.raises(ValueError):
        ledger.append([forecast(q10=0.9,q90=0.1)])


def test_issue_metadata_is_system_generated_and_idempotent(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    before = pd.Timestamp.now(tz="UTC")
    record = forecast(issued_at="2025-01-02T12:00:00Z", is_replay=False)
    assert ledger.append([record]) == 1
    saved = ledger.forecasts()[0]
    assert pd.Timestamp(saved["issued_at"]) >= before
    assert saved["is_replay"] is True
    # New issue-time calculations cannot change or duplicate the original issue.
    assert ledger.append([record]) == 0
    assert ledger.forecasts()[0]["issued_at"] == saved["issued_at"]


def test_replayed_forecasts_remain_visible_but_never_count_for_readiness(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([forecast(data_kind="historical", pipeline_version="p1", model_version="run1-m1")])
    p = prices().assign(source="finmind")
    ledger.settle(p, "2025-01-03T20:00:00+08:00", "s2")
    assert ledger.monitoring()["settled"] == 1
    assert ledger.monitoring()["settled_decision_dates"] == 1
    assert ledger.monitoring(eligible_only=True)["settled"] == 0
    assert ledger.monitoring(run_id="run1")["settled"] == 0
    assert ledger.monitoring(pipeline_version="p1")["settled"] == 0


def test_timely_real_forecasts_are_filtered_by_pipeline_and_market_session(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 1, 2, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("twquant.ledger.datetime", FrozenDatetime, raising=False)
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([
        forecast(data_kind="historical", pipeline_version="p1", model_version="run1-m1"),
        forecast(data_kind="historical", pipeline_version="p2", model_version="run2-m1"),
    ])
    ledger.settle(prices().assign(source="finmind"), "2025-01-03T20:00:00+08:00", "s2")
    assert ledger.monitoring()["settled"] == 2
    assert ledger.monitoring(pipeline_version="p1")["settled"] == 1
    assert ledger.monitoring(run_id="run2", eligible_only=True)["settled"] == 1
    assert ledger.monitoring(eligible_only=True)["settled_decision_dates"] == 1
    assert ledger.monitoring(pipeline_version="unknown")["total"] == 0


def test_settlement_rejects_source_mismatch_before_any_outcome(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([
        forecast(data_kind="historical", model_version="real-m1"),
        forecast(data_kind="synthetic", model_version="demo-m1"),
    ])
    with pytest.raises(ValueError, match="source|data.kind"):
        ledger.settle(prices().assign(source="synthetic_demo"), "2025-01-03T20:00:00+08:00", "s2")
    assert ledger.monitoring()["settled"] == 0


def test_settlement_rejects_already_known_target(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([forecast(decision_ts="2025-01-06T12:00:00Z")])
    with pytest.raises(ValueError, match="known|decision"):
        ledger.settle(prices(), "2025-01-06T20:00:00+08:00", "s2")
    assert ledger.monitoring()["settled"] == 0


def test_settlement_rejects_anchor_after_decision_date(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([forecast(anchor_date="2025-01-03")])
    with pytest.raises(ValueError, match="anchor|decision"):
        ledger.settle(prices(), "2025-01-06T20:00:00+08:00", "s2")
    assert ledger.monitoring()["settled"] == 0


def test_settlement_uses_single_adjustment_vintage(tmp_path):
    ledger = ForecastLedger(tmp_path / "ledger.sqlite")
    ledger.append([forecast()])
    p = prices().assign(adj_close=lambda frame: frame.adj_close * 7.0)
    assert ledger.settle(p, "2025-01-03T20:00:00+08:00", "rebased") == 1
    assert ledger.forecasts()[0]["actual_return"] == pytest.approx(.02)
