import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def module():
    assert importlib.util.find_spec('twquant.trade_first_change') is not None, 'Separate transaction first-change research is missing'
    from twquant import trade_first_change
    return trade_first_change


def hand_case(reset=False):
    from twquant.intraday_queue import _QueueHistory
    queues=np.full((1,2,5),100,dtype=int)
    if reset:queues[0,1,0]=1
    state=_QueueHistory(queues,np.zeros(1,dtype=int),np.ones(1,dtype=int),np.zeros(1),0.,np.zeros(1),np.ones(1,bool),{})
    state.last_trade_price=np.array([.5])
    draws=np.full((1,3,17),.5);draws[:,:,0]=.5;draws[:,:,2]=0.;draws[:,:,5]=0.;draws[:,:,6:16]=0.
    draws[0,:,1]=[0.,.99,0.] if reset else [.99,0.,0.]
    draws[0,:,16]=-np.expm1(-100*np.array([.002,.005,.003]))
    return state,draws


def advance(state,draws):
    return module().advance_trade_first_change(state,[draws],event_rate=100.,latency_seconds=.005,max_wait_seconds=60,flip_hazard=0.,size_weights=(.25,)*4)


def test_pre_emission_trade_changes_update_the_actual_emission_anchor():
    state,draws=hand_case();r=advance(state,draws)
    assert r['resolved'][0] and not r['timeout'][0]
    assert r['anchor_trade_price'][0]==-.5 and r['direction'][0]==1
    assert r['event_after_emission_seconds'][0]==pytest.approx(.002)
    assert r['pre_emission_price_changes'][0]==1
    assert state.last_trade_price[0]==.5 and state.position[0]==0


def test_same_price_prints_after_a_quote_reset_are_not_price_changes():
    state,draws=hand_case(reset=True);r=advance(state,draws)
    assert r['anchor_trade_price'][0]==.5 and r['direction'][0]==1
    assert r['event_after_emission_seconds'][0]==pytest.approx(.005)
    assert r['events_applied'][0]==3
    state,draws=hand_case();draws[0,:,1]=0.
    r=advance(state,draws[:,:2]);assert not r['resolved'][0] and not r['timeout'][0] and np.isnan(r['direction'][0])
    late=draws[:,:1].copy();late[:,:,16]=-np.expm1(-100*.1)
    # A different declared rate puts this event100seconds after the cutoff.
    r=module().advance_trade_first_change(state,[late],event_rate=.1,latency_seconds=.005,max_wait_seconds=60,flip_hazard=0.,size_weights=(.25,)*4)
    assert r['resolved'][0] and r['timeout'][0] and r['direction'][0]==0.


def panel(tickers=TRAINING_TICKERS,periods=40,**kwargs):
    m=module();from twquant.queue_first_change import QueueFirstScenario
    return m.simulate_trade_first_panel(tickers,pd.bdate_range('2020-01-02',periods=periods),QueueFirstScenario('test'),seed=17,**kwargs)


def test_observed_trade_features_are_future_invariant_and_index_has_no_transactions():
    a=panel((*TRAINING_TICKERS,'0050','IX0001'),future_seed=29,block_events=8)
    b=panel((*TRAINING_TICKERS,'0050','IX0001'),future_seed=29,block_events=32)
    pd.testing.assert_frame_equal(a,b)
    c=panel((*TRAINING_TICKERS,'0050','IX0001'),future_seed=43)
    features=[n for n in a if n.startswith(('f_book_','f_event_','f_trade_'))]
    pd.testing.assert_frame_equal(a[features],c[features])
    assert 'f_trade_location' in features and a.target_kind.eq('first_trade_change_before_deadline').all()
    ordinary=a.ticker.ne('IX0001');assert a.loc[ordinary,'simulation_resolved'].all()
    hit=ordinary&a.actual_return.ne(0)&a.actual_return.notna()
    assert a.loc[hit,'first_change_at'].gt(a.loc[hit,'prediction_at']).all()
    idx=a.loc[~ordinary];assert idx.actual_return.isna().all() and not idx.simulation_resolved.any()
    assert idx[features].isna().all().all() and idx.max_input_available_at.isna().all()
    assert idx.price_basis.eq('index_no_transaction_target').all()
    assert idx.diagnostic_anchor_trade_price.isna().all()
    primary=a[a.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True)
    pd.testing.assert_frame_equal(primary,panel(future_seed=29))


def test_shared_trade_models_use_only_observables_and_reject_midquote_or_endpoint_labels():
    m=module();frame=panel(periods=60);fit_at=frame.label_available_at.max()+pd.Timedelta(days=1)
    for name in ('prior','spread_bounce','trade_logistic','trade_hist'):
        model=m.fit_trade_first_model(frame,name,fit_at=fit_at);probe=frame.tail(20)
        altered=probe.assign(ticker='1216',actual_return=999.,diagnostic_anchor_trade_price=999.,f_book_oracle_future=999.)
        np.testing.assert_array_equal(model.predict_proba(probe),model.predict_proba(altered))
        np.testing.assert_array_equal(model.predict_proba(probe.assign(input_ready=False))[:,1],np.full(len(probe),model.prior))
        assert model.training_details['target_kind']=='first_trade_change_before_deadline'
        assert not model.training_details['external_targets_used'] and 'timeout' in model.training_details['labels']
        assert not any('diagnostic' in n or 'oracle' in n for n in model.training_details['features'])
        if name=='spread_bounce':np.testing.assert_array_equal(model.predict_proba(probe)[:,1],probe.f_trade_location.lt(0).astype(float))
    with pytest.raises(ValueError,match='target'):
        m.fit_trade_first_model(frame.assign(target_kind='first_mid_change_before_deadline'),'trade_logistic',fit_at=fit_at)
    with pytest.raises(ValueError,match='fixed ten'):
        m.fit_trade_first_model(frame.assign(ticker='1216'),'trade_logistic',fit_at=fit_at)
    with pytest.raises(ValueError,match='mature'):
        m.fit_trade_first_model(frame,'trade_logistic',fit_at=frame.decision_at.min())
    from twquant.first_change import fit_first_change_model
    from twquant.intraday_models import fit_intraday_model
    with pytest.raises(ValueError,match='target'):fit_first_change_model(frame,'prior',fit_at=fit_at)
    with pytest.raises(ValueError,match='target'):fit_intraday_model(frame,'prior',fit_at=fit_at)


def test_trade_audit_retains_missing_unknown_and_timeout_without_relabeling_midquote():
    m=module();members=[str(1000+i) for i in range(50)];p=panel(periods=1).iloc[[0]*50].copy().reset_index(drop=True)
    p['ticker']=members;p['actual_return']=.0001;p['first_change_at']=p.decision_at+pd.Timedelta(milliseconds=100)
    p=p.assign(probability_up=1.,decision_threshold=.5,model_version='one-shared-model')
    p.loc[0,['actual_return','first_change_at','timeout']]=[0.,pd.NaT,True]
    p.loc[1,['actual_return','first_change_at']]=[np.nan,pd.NaT]
    p=p.drop(index=2);calendar=p.decision_at.unique()
    a=m.audit_trade_first_predictions(p,members,expected_decisions=calendar,max_wait_seconds=60,min_dates=1)
    assert a['target_kind']=='first_trade_change_before_deadline' and a['target_scope_confirmed']
    assert a['expected_predictions']==50 and a['missing_predictions']==1 and a['unresolved_answers']==2 and a['timeout_predictions']==1
    assert a['overall_accuracy']==47/50 and not a['empirical_target_met'] and not a['target_feasibility_established']
    from twquant.first_change import audit_first_change_predictions
    with pytest.raises(ValueError,match='target'):audit_first_change_predictions(p,members,expected_decisions=calendar,max_wait_seconds=60,min_dates=1)
    with pytest.raises(ValueError,match='target'):m.audit_trade_first_predictions(p.assign(target_kind='first_mid_change_before_deadline'),members,expected_decisions=calendar,max_wait_seconds=60,min_dates=1)


def test_trade_first_cli_is_offline_and_preserves_the_index_query(tmp_path,capsys,monkeypatch):
    from twquant import cli
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'config.json';config.write_text(json.dumps(dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],max_wait_seconds=60,max_events=2048,candidates=['prior','spread_bounce','trade_logistic','trade_hist'],scenarios=[{'name':'minimal'}])))
    monkeypatch.setattr(cli,'load_dotenv',lambda:pytest.fail('Offline trade study must not read provider credentials'))
    out=tmp_path/'study'
    assert cli.main(['direction-trade-first-study','--config',str(config),'--membership',str(membership),'--out',str(out)])==0
    result=json.loads(capsys.readouterr().out);assert result['models_trained']==4
    protocol=json.loads((out/'protocol.json').read_text());report=json.loads((out/'report.json').read_text())
    assert protocol['prediction_target_starts_at']=='emission' and protocol['target_kind']=='first_trade_change_before_deadline'
    run=report['experiments'][0];assert run['selected_test_audit']['expected_predictions']==50*126
    assert run['transfer']['IX0001']['unresolved_answers']==126 and not run['transfer']['IX0001']['tradable']
    assert not report['empirical_target_met'] and not report['target_feasibility_established']


def test_array_inference_preserves_dataframe_rounding_missing_inputs_and_immutable_queries():
    m=module();frame=panel(periods=40)
    model=m.fit_trade_first_model(frame,'spread_bounce',fit_at=frame.label_available_at.max()+pd.Timedelta(days=1))
    assert hasattr(model,'predict_locations'), 'The shared bounce model needs a compact observed-location interface'
    locations=np.array([-3.,-1.,-1e-14,0.,1e-14,1.,3.,np.nan,np.inf])
    ready=np.array([True]*7+[False,False]);before=locations.copy()
    probe=frame.iloc[:len(locations)].assign(f_trade_location=locations,input_ready=ready)
    actual=model.predict_locations(locations,input_ready=ready)
    np.testing.assert_array_equal(actual,model.predict_proba(probe))
    np.testing.assert_array_equal(actual[:,1],np.array([1.,1.,0.,0.,0.,0.,0.,model.prior,model.prior]))
    np.testing.assert_array_equal(locations,before)
    np.testing.assert_array_equal(actual.sum(axis=1),np.ones(len(locations)))
    assert model.predict_locations(np.array([],float),input_ready=np.array([],bool)).shape==(0,2)


def test_array_inference_rejects_invalid_observations_and_non_bounce_models():
    m=module();frame=panel(periods=40);fit_at=frame.label_available_at.max()+pd.Timedelta(days=1)
    model=m.fit_trade_first_model(frame,'spread_bounce',fit_at=fit_at)
    assert hasattr(model,'predict_locations'), 'The shared bounce model needs a compact observed-location interface'
    for locations,ready in (([[1.]],np.array([True])),([1.],np.array([True,False])),([1.],np.array([1])),(['1'],np.array([True])),([np.nan],np.array([True])),([np.inf],np.array([True]))):
        with pytest.raises(ValueError,match='location|ready'):model.predict_locations(locations,input_ready=ready)
    prior=m.fit_trade_first_model(frame,'prior',fit_at=fit_at)
    with pytest.raises(ValueError,match='spread.bounce'):prior.predict_locations(np.array([-1.]),input_ready=np.array([True]))
