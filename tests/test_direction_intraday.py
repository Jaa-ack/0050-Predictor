import importlib.util

import numpy as np
import pandas as pd
import pytest


TICKERS = tuple(str(1000+i) for i in range(50))


def panel(returns=(-.01, .01), days=4):
    times=pd.date_range('2026-01-05 10:00', periods=days, freq='B', tz='Asia/Taipei')
    rows=[]
    for decision in times:
        for ticker in TICKERS:
            rows.append(dict(ticker=ticker,decision_at=decision,
                target_at=decision+pd.Timedelta(seconds=30),
                prediction_at=decision-pd.Timedelta(milliseconds=1),
                max_input_available_at=decision-pd.Timedelta(milliseconds=2),
                actual_return=returns[len(rows)//50 % len(returns)],
                probability_up=.1, decision_threshold=.5,model_version='shared-fixture'))
    return pd.DataFrame(rows),times


def audit(frame, times, **kwargs):
    assert importlib.util.find_spec('twquant.direction_intraday') is not None, 'Full-calendar intraday audit is missing'
    from twquant.direction_intraday import audit_intraday_predictions
    return audit_intraday_predictions(frame,TICKERS,expected_decisions=times,
        horizon_seconds=30,evidence_kind='synthetic',min_dates=4,**kwargs)


def test_flat_prices_cannot_turn_a_majority_nonup_rule_into_75pct_direction_accuracy():
    frame,times=panel(returns=(0.,0.,0.,-.01))
    report=audit(frame,times)
    assert report['overall_accuracy']==.25
    assert report['flat_fraction']==.75
    assert report['up_vs_nonup_accuracy_diagnostic']==1.
    assert report['prediction_coverage']==report['answer_coverage']==1.
    assert not report['full_coverage_sample_gate_met']
    assert not report['real_world_target_verified']


def test_all_dates_and_weak_stocks_remain_in_the_intraday_denominator():
    frame,times=panel(returns=(-.01,))
    frame.loc[frame.ticker.isin(TICKERS[:5]),'probability_up']=.9
    report=audit(frame,times)
    assert report['overall_accuracy']==.9
    assert report['minimum_stock_accuracy']==report['worst_five_accuracy']==0.
    assert report['five_stock_combinations']==2118760
    assert set(report['worst_five_tickers'])==set(TICKERS[:5])
    incomplete=frame.drop(frame.index[0]).copy()
    incomplete.loc[incomplete.index[0],'actual_return']=np.nan
    incomplete.loc[incomplete.index[1],'probability_up']=np.nan
    missing=audit(incomplete,times)
    assert missing['expected_predictions']==200
    assert missing['missing_predictions']==1
    assert missing['unresolved_answers']==2
    assert missing['prediction_coverage']==198/200
    assert missing['answer_coverage']==198/200
    assert not missing['full_coverage_sample_gate_met']


def test_late_inputs_or_emissions_receive_no_credit_and_confidence_does_not_filter_days():
    frame,times=panel()
    frame['probability_up']=np.where(frame.actual_return>0,.51,.49)
    assert audit(frame,times)['full_coverage_sample_gate_met']
    frame.loc[0,'max_input_available_at']=frame.loc[0,'prediction_at']+pd.Timedelta(microseconds=1)
    frame.loc[1,'prediction_at']=frame.loc[1,'decision_at']+pd.Timedelta(microseconds=1)
    report=audit(frame,times)
    assert report['invalid_predictions']==2
    assert report['prediction_coverage']==198/200
    assert report['overall_accuracy']==198/200
    assert not report['full_coverage_sample_gate_met']
    assert not report['empirical_target_met']


@pytest.mark.parametrize('change', ['mixed_model','mixed_threshold','wrong_target','naive_time','duplicate','external'])
def test_incompatible_models_targets_clocks_and_universes_are_rejected(change):
    frame,times=panel()
    if change=='mixed_model':frame.loc[0,'model_version']='external-model'
    if change=='mixed_threshold':frame.loc[0,'decision_threshold']=.6
    if change=='wrong_target':frame.loc[0,'target_at']+=pd.Timedelta(seconds=1)
    if change=='naive_time':frame['prediction_at']=frame.prediction_at.dt.tz_localize(None)
    if change=='duplicate':frame=pd.concat([frame,frame.iloc[[0]]],ignore_index=True)
    if change=='external':frame.loc[0,'ticker']='0050'
    with pytest.raises(ValueError):audit(frame,times)


def test_empty_predictions_still_audit_the_declared_calendar_and_universe():
    frame,times=panel()
    report=audit(frame.iloc[:0],times)
    assert report['expected_predictions']==report['missing_predictions']==200
    assert report['overall_accuracy']==report['prediction_coverage']==report['answer_coverage']==0.
    assert report['unresolved_answers']==200
    assert report['planned_training_tickers']==['2330','2454','2317','2308','3711','1303','2383','2881','2891','3037']
    assert report['training_isolation_verified'] is False


@pytest.mark.parametrize('clock', ['08:59:59','13:24:45','13:30:00'])
def test_fixed_clock_targets_cannot_cross_out_of_the_continuous_trading_session(clock):
    frame,times=panel()
    shifted=times.normalize()+pd.Timedelta(clock)
    for column in ('decision_at','target_at','prediction_at','max_input_available_at'):
        frame[column]+=shifted[0]-times[0]
    with pytest.raises(ValueError,match='continuous'):
        audit(frame,shifted)
