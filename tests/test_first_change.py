import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def module():
    assert importlib.util.find_spec('twquant.first_change') is not None, 'Bounded first-change research is missing'
    from twquant import first_change
    return first_change


def panel(tickers=TRAINING_TICKERS,**kwargs):
    m=module();dates=kwargs.pop('dates',pd.bdate_range('2020-01-02',periods=40))
    scenario=kwargs.pop('scenario',m.FirstChangeScenario('fixture'))
    return m.simulate_first_change_panel(tickers,dates,scenario,**kwargs)


def test_future_hitting_times_never_change_inputs_and_quantity_units_cancel():
    m=module();a=panel();b=panel(future_seed=97)
    cols=['ticker','date','decision_at','input_ready','f_book_imbalance_1','bid_depth','ask_depth']
    pd.testing.assert_frame_equal(a[cols],b[cols])
    assert not a.actual_return.equals(b.actual_return)
    c=panel(scenario=m.FirstChangeScenario('scaled',quantity_units=100.))
    np.testing.assert_array_equal(a.f_book_imbalance_1,c.f_book_imbalance_1)
    np.testing.assert_array_equal(a.actual_return,c.actual_return)
    pd.testing.assert_series_equal(a.first_change_at,c.first_change_at)
    assert np.allclose(c.bid_depth,a.bid_depth*100)
    assert set(a.target_kind)=={'first_mid_change_before_deadline'}
    assert (a.first_change_at.dropna()>a.loc[a.first_change_at.notna(),'decision_at']).all()
    assert a.loc[a.first_change_at.notna(),'first_change_at'].le(a.loc[a.first_change_at.notna(),'target_at']).all()
    assert a.label_available_at.gt(a.target_at).all()
    assert a.max_input_available_at.le(a.prediction_at).all()


def test_exponential_depth_formula_includes_timeouts_and_independent_missing_inputs():
    r=module().first_change_feasibility(queue_shape=1.,diffusion_scale=1.,max_wait_seconds=60,availability=1.)
    assert r['unbounded_direction_accuracy']==pytest.approx(1-np.log(2)/np.pi)
    assert r['complete_coverage_accuracy_lower']>.75
    assert 0<r['timeout_probability']<.02
    low=module().first_change_feasibility(queue_shape=1.,diffusion_scale=1.,max_wait_seconds=60,availability=.75)
    assert low['complete_coverage_accuracy_upper']<.75
    balanced=module().first_change_feasibility(queue_shape=2.,diffusion_scale=1.,max_wait_seconds=60,availability=1.)
    assert balanced['unbounded_direction_accuracy']<.75
    assert not r['market_calibrated'] and not r['target_feasibility_established']
    assert r['target_scope_confirmed'] is True


def test_external_depth_and_activity_overrides_cannot_change_primary_examples_or_fit():
    m=module();universe=(*TRAINING_TICKERS,'1216','0050','IX0001')
    a=panel(universe);b=panel(universe,scenario=m.FirstChangeScenario('external',external_queue_shapes=(('1216',4.),),external_diffusion_scales=(('1216',.2),)))
    pd.testing.assert_frame_equal(a[a.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True),b[b.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True))
    index=a[a.ticker.eq('IX0001')]
    assert not index.input_ready.any() and index.bid_depth.isna().all() and index.ask_depth.isna().all()
    assert index.actual_return.notna().all() and set(index.price_basis)=={'abstract_index_points_no_book'}
    primary=a[a.ticker.isin(TRAINING_TICKERS)]
    fit_at=primary.label_available_at.max()+pd.Timedelta(days=1)
    model=m.fit_first_change_model(primary,'symmetric_logistic',fit_at=fit_at)
    probe=a.tail(20);before=model.predict_proba(probe)
    np.testing.assert_array_equal(before,model.predict_proba(probe.assign(actual_return=999.,ticker='UNKNOWN',first_change_at=probe.target_at)))
    np.testing.assert_allclose(model.predict_proba(probe.assign(input_ready=False))[:,1],model.prior)
    with pytest.raises(ValueError,match='fixed ten'):m.fit_first_change_model(a,'symmetric_logistic',fit_at=fit_at)
    with pytest.raises(ValueError,match='mature'):m.fit_first_change_model(primary,'symmetric_logistic',fit_at=primary.label_available_at.max())
    with pytest.raises(ValueError,match='target'):m.fit_first_change_model(primary.assign(target_kind='fixed_endpoint'),'symmetric_logistic',fit_at=fit_at)
    with pytest.raises(ValueError):m.FirstChangeScenario('bad',external_queue_shapes=(('2330',2.),))


def audit_fixture():
    m=module();dates=pd.bdate_range('2020-01-02',periods=3)
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    p=panel(members,dates=dates)
    p['probability_up']=p.actual_return.gt(0).astype(float)
    p['model_version']='shared_first_fixture';p['decision_threshold']=.5
    return p,members,dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)


def test_bounded_first_change_audit_keeps_timeouts_missing_rows_and_missing_answers():
    m=module();p,members,decisions=audit_fixture()
    row=p.index[0]
    p.loc[row,['actual_return','first_change_at','timeout']]=[0.,pd.NaT,True]
    p=p.drop(index=1)
    p.loc[2,'actual_return']=np.nan
    r=m.audit_first_change_predictions(p,members,expected_decisions=decisions,max_wait_seconds=60,min_dates=3)
    assert r['expected_predictions']==150 and r['missing_predictions']==1 and r['unresolved_answers']==2
    assert r['timeout_predictions']>=1
    assert r['overall_accuracy']<=147/150
    assert r['five_stock_combinations']==2118760
    assert not r['empirical_target_met'] and r['target_scope_confirmed'] is True
    bad=p.copy();known=bad.index[bad.actual_return.ne(0)&bad.actual_return.notna()][0]
    bad.loc[known,'first_change_at']=bad.loc[known,'target_at']+pd.Timedelta(seconds=1)
    with pytest.raises(ValueError,match='event'):m.audit_first_change_predictions(bad,members,expected_decisions=decisions,max_wait_seconds=60)


def test_clock_endpoint_audit_rejects_explicit_first_change_labels():
    from twquant.direction_intraday import audit_intraday_predictions
    p,members,decisions=audit_fixture()
    with pytest.raises(ValueError,match='target'):
        audit_intraday_predictions(p,members,expected_decisions=decisions,horizon_seconds=60,min_dates=3)


def test_clock_endpoint_fitting_rejects_first_change_labels_even_with_matching_deadlines():
    from twquant.intraday_models import fit_intraday_model
    p=panel();p['horizon_seconds']=p.max_wait_seconds
    with pytest.raises(ValueError,match='target'):
        fit_intraday_model(p,'history_logistic',fit_at=p.label_available_at.max()+pd.Timedelta(days=1))


def test_first_change_cli_uses_fixed_ten_only_and_never_loads_credentials(tmp_path,monkeypatch,capsys):
    import twquant.cli as cli
    def forbidden():raise AssertionError('Offline first-change study cannot read credentials')
    monkeypatch.setattr(cli,'load_dotenv',forbidden)
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'first.json'
    config.write_text(json.dumps(dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],max_wait_seconds=[30],scenarios=[dict(name='fixture')])))
    output=tmp_path/'study';args=['direction-first-change-study','--config',str(config),'--membership',str(membership),'--out',str(output)]
    assert cli.main(args)==0
    response=json.loads(capsys.readouterr().out)
    assert response['target_scope_confirmed'] is True
    protocol=json.loads((output/'protocol.json').read_text());r=json.loads((output/'report.json').read_text())
    assert protocol['training_tickers']==list(TRAINING_TICKERS)
    assert protocol['target_scope_confirmed'] is True and r['target_scope_confirmed'] is True
    assert len(protocol['source_sha256'])==64 and r['models_trained']==3
    assert r['experiments'][0]['selected_test_audit']['expected_predictions']==126*50
    assert not r['target_feasibility_established']
    assert cli.main(args)==2
    assert 'fresh' in capsys.readouterr().err
