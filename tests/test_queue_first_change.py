import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def module():
    assert importlib.util.find_spec('twquant.queue_first_change') is not None, 'Poisson-clock first-change process is missing'
    from twquant import queue_first_change
    return queue_first_change


def hand_case():
    from twquant.intraday_queue import _QueueHistory
    queues=np.full((1,2,5),5,dtype=int);queues[:,:,0]=1
    state=_QueueHistory(queues,np.zeros(1,dtype=int),np.ones(1,dtype=int),np.zeros(1),0.,np.zeros(1),np.ones(1,bool),{})
    draws=np.full((1,3,17),.5)
    draws[:,:,0]=.5;draws[:,:,2]=0.;draws[:,:,6:16]=0.
    draws[0,0,1]=0.;draws[0,0,5]=0.
    draws[0,1:,1]=.99;draws[0,1:,5]=.99
    draws[0,:,16]=-np.expm1(-100*np.array([.002,.005,.003]))
    return state,draws


def test_reset_before_emission_is_followed_by_a_new_future_first_change():
    m=module();state,draws=hand_case()
    r=m.advance_first_change(state,[draws],event_rate=100.,latency_seconds=.005,max_wait_seconds=1,flip_hazard=0.,size_weights=(.25,)*4)
    assert r['resolved'][0] and not r['timeout'][0]
    assert r['pre_emission_events'][0]==r['pre_emission_price_changes'][0]==1
    assert r['anchor_position'][0]==1
    assert r['direction'][0]==-1
    assert r['event_after_emission_seconds'][0]==pytest.approx(.005)
    assert state.position[0]==0 and state.queues[0,1,0]==1


def test_truncated_simulation_retains_unknown_answers_instead_of_inventing_timeouts():
    m=module();state,draws=hand_case()
    r=m.advance_first_change(state,[draws[:,:2]],event_rate=100.,latency_seconds=.005,max_wait_seconds=1,flip_hazard=0.,size_weights=(.25,)*4)
    assert not r['resolved'][0] and not r['timeout'][0]
    assert np.isnan(r['direction'][0])
    late=draws[:,:1].copy();late[:,:,16]=-np.expm1(-2.)
    r=m.advance_first_change(state,[late],event_rate=1.,latency_seconds=0.,max_wait_seconds=1,flip_hazard=0.,size_weights=(.25,)*4)
    assert r['resolved'][0] and r['timeout'][0] and r['direction'][0]==0.


def test_weight_rounding_cannot_create_an_undeclared_fifth_lot_size():
    m=module();state,draws=hand_case();state.queues[:,:,0]=5
    draws=draws[:,:1].copy();draws[:,:,5]=np.nextafter(1.,0.)
    r=m.advance_first_change(state,[draws],event_rate=100.,latency_seconds=0.,max_wait_seconds=1,
        flip_hazard=0.,size_weights=(.25,.25,.25,.25-5e-13))
    assert not r['resolved'][0] and np.isnan(r['direction'][0])


def simulate(tickers=TRAINING_TICKERS,**kwargs):
    m=module();dates=kwargs.pop('dates',pd.bdate_range('2020-01-02',periods=15))
    scenario=kwargs.pop('scenario',m.QueueFirstScenario('fixture'))
    return m.simulate_queue_first_change_panel(tickers,dates,scenario,**kwargs)


def test_event_clocks_history_and_index_roles_are_causal_and_chunk_invariant():
    a=simulate((*TRAINING_TICKERS,'0050','1216','IX0001'),future_seed=29,block_events=8)
    b=simulate((*TRAINING_TICKERS,'0050','1216','IX0001'),future_seed=97,block_events=8)
    known=['ticker','date','input_ready','max_input_available_at',*[c for c in a if c.startswith(('f_book_','f_event_'))]]
    pd.testing.assert_frame_equal(a[known],b[known])
    assert not a.first_change_at.equals(b.first_change_at)
    c=simulate((*TRAINING_TICKERS,'0050','1216','IX0001'),future_seed=29,block_events=32)
    pd.testing.assert_frame_equal(a,c)
    expanded=simulate((*TRAINING_TICKERS,'0050','1216','IX0001','2002'),future_seed=29,dates=pd.bdate_range('2020-01-02',periods=16))
    pd.testing.assert_frame_equal(a,expanded[expanded.date.le(a.date.max())&expanded.ticker.isin(a.ticker)].reset_index(drop=True))
    assert a.simulation_resolved.all()
    hits=a.first_change_at.notna()
    assert a.loc[hits,'first_change_at'].gt(a.loc[hits,'prediction_at']).all()
    assert a.loc[a.input_ready,'max_input_available_at'].le(a.loc[a.input_ready,'prediction_at']).all()
    assert a.loc[~a.input_ready,'max_input_available_at'].isna().all()
    assert a.target_at.eq(a.decision_at+pd.Timedelta(seconds=60)).all()
    assert not any('mean_100' in col for col in a if col.startswith('f_book_'))
    index=a[a.ticker.eq('IX0001')]
    assert not index.input_ready.any() and index.bid_depth.isna().all() and index.ask_depth.isna().all()


def test_external_rates_and_lot_profiles_never_change_primary_inputs_or_labels():
    m=module();ids=(*TRAINING_TICKERS,'1216')
    a=simulate(ids);b=simulate(ids,scenario=m.QueueFirstScenario('shift',external_event_rates=(('1216',1000.),),external_size_weights=(('1216',(.25,)*4),)))
    pd.testing.assert_frame_equal(a[a.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True),b[b.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True))
    with pytest.raises(ValueError):m.QueueFirstScenario('bad',external_event_rates=(('2330',1.),))
    with pytest.raises(ValueError):m.QueueFirstScenario('bad',size_weights=(.1,)*4)
    with pytest.raises(ValueError):m.QueueFirstScenario('bad',latency_seconds=-.001)


def test_shared_event_history_model_cannot_read_hidden_state_or_future_answers():
    m=module();frame=simulate(dates=pd.bdate_range('2020-01-02',periods=40))
    from twquant.first_change import fit_first_change_model
    model=fit_first_change_model(frame,'event_history_logistic',fit_at=frame.label_available_at.max()+pd.Timedelta(days=1))
    probe=frame.tail(20);before=model.predict_proba(probe)
    changed=probe.assign(ticker='1216',actual_return=999.,diagnostic_anchor_position=999.,f_event_oracle_future=999.)
    np.testing.assert_array_equal(before,model.predict_proba(changed))
    np.testing.assert_allclose(model.predict_proba(probe.assign(input_ready=False))[:,1],model.prior)
    assert model.training_details['history_window_unit']=='events'
    assert 'timeout' in model.training_details['labels']
    assert model.training_details['timeout_training_rows']==int(frame.actual_return.eq(0).sum())
    assert 'flat_training_rows' not in model.training_details
    assert not any('diagnostic' in c or 'oracle' in c for c in model.training_details['features'])
    with pytest.raises(ValueError,match='events'):
        model.predict_proba(probe.assign(history_window_unit='seconds'))


def test_queue_first_cli_freezes_the_event_clock_and_never_loads_credentials(tmp_path,capsys,monkeypatch):
    import twquant.cli as cli
    def forbidden():raise AssertionError('Offline queue-clock research cannot load credentials')
    monkeypatch.setattr(cli,'load_dotenv',forbidden)
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'study.json'
    config.write_text(json.dumps(dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],max_wait_seconds=60,scenarios=[dict(name='fixture')],candidates=['prior','logistic','symmetric_logistic','event_history_logistic'])))
    output=tmp_path/'output';args=['direction-queue-first-study','--config',str(config),'--membership',str(membership),'--out',str(output)]
    assert cli.main(args)==0
    response=json.loads(capsys.readouterr().out);assert response['target_scope_confirmed']
    protocol=json.loads((output/'protocol.json').read_text());r=json.loads((output/'report.json').read_text())
    assert protocol['history_window_unit']=='events' and protocol['prediction_target_starts_at']=='emission'
    assert r['models_trained']==4 and r['experiments'][0]['selected_test_audit']['expected_predictions']==126*50
    assert not r['market_calibrated'] and not r['target_feasibility_established']
