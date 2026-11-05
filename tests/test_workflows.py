import pandas as pd

from twquant.workflows import assess_readiness


def test_synthetic_data_can_never_pass_feasibility_gate():
    p=pd.DataFrame({"date":pd.bdate_range("2010-01-01",periods=2600),"source":"demo",
                    "ticker":"2330","availability_basis":"observed"})
    gate=assess_readiness(p,{"feasibility":{"passed":True}})
    assert gate["passed"] is False
    assert "synthetic_data" in gate["blockers"]


def test_reconstructed_timestamps_and_no_shadow_record_block_promotion():
    p=pd.DataFrame({"date":pd.bdate_range("2010-01-01",periods=2600),"source":"finmind",
                    "ticker":"2330","availability_basis":"reconstructed_eod"})
    gate=assess_readiness(p,{"feasibility":{"passed":True}})
    assert gate["passed"] is False
    assert "point_in_time_history_unverified" in gate["blockers"]
    assert "paper_track_record_insufficient" in gate["blockers"]


def test_arbitrary_tickers_and_unverified_adjustments_cannot_pass_gate():
    p=pd.concat([pd.DataFrame({"date":pd.bdate_range("2010-01-01",periods=2600),"source":"csv",
                    "ticker":f"X{i}","availability_basis":"observed",
                    "adjustment_source":"user_supplied_unverified"}) for i in range(11)])
    gate=assess_readiness(p,{"feasibility":{"passed":True}},
        {"by_horizon":{"1":{"count":500}},"settled_decision_dates":60})
    assert not gate["passed"]
    assert "required_target_universe_missing" in gate["blockers"]


def test_synthetic_demo_source_is_always_labeled_synthetic():
    from twquant.workflows import data_kind
    assert data_kind(pd.DataFrame({"source":["synthetic_demo"]})) == "synthetic"


def test_bad_live_probability_and_coverage_block_promotion():
    p=pd.DataFrame({"date":pd.bdate_range("2010-01-01",periods=2600),"source":"finmind",
                    "ticker":"2330","availability_basis":"observed"})
    gate=assess_readiness(p,{"feasibility":{"passed":True}},
        {"by_horizon":{"1":{"count":600,"brier":0.4,"coverage":0.3}},"settled_decision_dates":60})
    assert "paper_prediction_quality_insufficient" in gate["blockers"]


def test_external_sources_with_unverified_historical_timing_block_promotion():
    p=pd.DataFrame({'ticker':['2330'],'date':pd.to_datetime(['2025-01-02']),'source':['csv'],
                    'availability_basis':['observed']})
    gate=assess_readiness(p,{'data_quality':{'external_history_verified':False}})
    assert 'external_history_time_unverified' in gate['blockers']
