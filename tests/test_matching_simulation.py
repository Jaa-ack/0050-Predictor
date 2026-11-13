import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def module():
    assert importlib.util.find_spec('twquant.matching_simulation') is not None, 'Chronological matching-book research is missing'
    from twquant import matching_simulation
    return matching_simulation


def panel(tickers=TRAINING_TICKERS,periods=40,scenario=None,**kwargs):
    m=module()
    return m.simulate_matching_panel(tickers,pd.bdate_range('2020-01-02',periods=periods),scenario or m.MatchingScenario('fixture'),seed=17,**kwargs)


def test_matching_observables_are_causal_and_index_has_no_book_or_trade_answer():
    universe=('2330','1216','IX0001');a=panel(universe,periods=12,future_seed=29,block_events=8)
    b=panel(universe,periods=12,future_seed=29,block_events=32);pd.testing.assert_frame_equal(a,b)
    c=panel(universe,periods=12,future_seed=43)
    features=[n for n in a if n.startswith(('f_book_','f_event_','f_trade_'))]
    pd.testing.assert_frame_equal(a[features],c[features])
    ordinary=a.ticker.ne('IX0001');q=a[ordinary]
    assert q.simulation_resolved.all() and q.input_ready.all()
    assert (q.f_trade_spread_ticks==q.observed_ask-q.observed_bid).all()
    assert (q.f_trade_spread_ticks>1).any()
    assert (q.f_trade_location==2*(q.observed_last_trade-(q.observed_bid+q.observed_ask)/2)/(q.observed_ask-q.observed_bid)).all()
    hit=q.actual_return.ne(0);assert q.loc[hit,'first_change_at'].gt(q.loc[hit,'prediction_at']).all()
    np.testing.assert_array_equal(q.loc[hit,'actual_return'],(q.loc[hit,'first_change_price']-q.loc[hit,'diagnostic_anchor_trade_price'])/q.loc[hit,'diagnostic_anchor_trade_price'])
    idx=a[~ordinary];assert idx[features].isna().all().all() and idx.actual_return.isna().all()
    assert idx.observed_bid.isna().all() and idx.diagnostic_anchor_trade_price.isna().all() and not idx.simulation_resolved.any()


def test_external_matching_activity_never_changes_primary_book_histories_or_future_answers():
    m=module();a=panel(('2330','1216'),periods=8,future_seed=29)
    scenario=m.MatchingScenario('external',external_event_rates=(('1216',1000.),),external_large_order_fractions=(('1216',.2),))
    b=panel(('2330','1216'),periods=8,scenario=scenario,future_seed=29)
    pd.testing.assert_frame_equal(a[a.ticker.eq('2330')].reset_index(drop=True),b[b.ticker.eq('2330')].reset_index(drop=True))
    pd.testing.assert_frame_equal(a[a.ticker.eq('2330')].reset_index(drop=True),panel(('2330',),periods=8,future_seed=29))
    with pytest.raises(ValueError,match='external'):m.MatchingScenario('bad',external_large_order_fractions=(('2330',.2),))
    with pytest.raises(ValueError,match='fraction'):m.MatchingScenario('bad',market_fraction=.9,cancel_fraction=.3)
    with pytest.raises(ValueError,match='liquidity'):m.MatchingScenario('bad',queue={'liquidity_response':1.})


def test_matching_models_receive_variable_spread_and_causal_fill_normalized_depth_only():
    from twquant.trade_first_change import fit_trade_first_model
    frame=panel(periods=40);at=frame.label_available_at.max()+pd.Timedelta(days=1)
    for name in ('spread_bounce','trade_logistic','trade_hist'):
        model=fit_trade_first_model(frame,name,fit_at=at);probe=frame.tail(20)
        altered=probe.assign(actual_return=999.,ticker='1216',diagnostic_anchor_trade_price=999.,f_trade_oracle_future=999.)
        np.testing.assert_array_equal(model.predict_proba(probe),model.predict_proba(altered))
        if name!='spread_bounce':
            assert {'f_book_trade_spread_ticks','f_book_trade_bid_depth_to_fill','f_book_trade_ask_depth_to_fill'}<=set(model.training_details['features'])
    from twquant.first_change import fit_first_change_model
    with pytest.raises(ValueError,match='target'):fit_first_change_model(frame,'prior',fit_at=at)


def test_matching_study_cli_preserves_daily_denominators_and_never_loads_credentials(tmp_path,capsys,monkeypatch):
    from twquant import cli
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'config.json';config.write_text(json.dumps(dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],max_wait_seconds=60,max_events=2048,candidates=['prior','spread_bounce','trade_logistic','trade_hist'],scenarios=[{'name':'minimal'}])))
    monkeypatch.setattr(cli,'load_dotenv',lambda:pytest.fail('Offline matching research cannot load credentials'))
    out=tmp_path/'study';assert cli.main(['direction-matching-study','--config',str(config),'--membership',str(membership),'--out',str(out)])==0
    result=json.loads(capsys.readouterr().out);assert result['models_trained']==4
    protocol=json.loads((out/'protocol.json').read_text());report=json.loads((out/'report.json').read_text())
    assert protocol['matching_rule']=='ordered_opposite_levels' and protocol['prediction_target_starts_at']=='emission'
    run=report['experiments'][0];assert run['selected_test_audit']['expected_predictions']==50*126
    assert run['transfer']['IX0001']['unresolved_answers']==126 and not run['transfer']['IX0001']['tradable']
    assert not report['empirical_target_met'] and not report['target_feasibility_established']


def test_partial_matching_feature_profile_cannot_silently_change_a_fitted_schema():
    from twquant.trade_first_change import fit_trade_first_model
    frame=panel(periods=30).drop(columns=['f_trade_bid_depth_to_fill'])
    with pytest.raises(ValueError,match='profile'):
        fit_trade_first_model(frame,'trade_logistic',fit_at=frame.label_available_at.max()+pd.Timedelta(days=1))


def test_expanding_unused_known_depth_preserves_the_same_observed_and_future_path():
    m=module()
    a=panel(('2330',),periods=8,scenario=m.MatchingScenario('small',grid_ticks=128),future_seed=29)
    b=panel(('2330',),periods=8,scenario=m.MatchingScenario('large',grid_ticks=256),future_seed=29)
    assert a.simulation_resolved.all() and b.simulation_resolved.all()
    pd.testing.assert_frame_equal(a,b)
