import importlib.util

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def inputs(tickers=('2330',),days=2):
    decisions=pd.date_range('2026-01-05 10:00',periods=days,freq='B',tz='Asia/Taipei')
    quotes=[];checkpoints=[]
    for day,decision in enumerate(decisions):
        sign=1 if day%2 else -1
        for ticker in tickers:
            for step in range(100):
                stamp=decision-pd.Timedelta(seconds=100-step)
                mid=100+sign*step*.01
                quotes.append(dict(ticker=ticker,state_at=stamp,received_at=stamp+pd.Timedelta(milliseconds=10),
                    bid_prices=[mid-.5-i for i in range(5)],ask_prices=[mid+.5+i for i in range(5)],
                    bid_sizes=[20+sign*5+i for i in range(5)],ask_sizes=[20-sign*5+i for i in range(5)],
                    is_trial=False,is_continuous=True,source='synthetic_fixture'))
            anchor=100+sign*.99
            for seconds,mid in [(0,anchor),(30,anchor+sign)]:
                stamp=decision+pd.Timedelta(seconds=seconds)
                checkpoints.append(dict(ticker=ticker,price_at=stamp,available_at=stamp+pd.Timedelta(milliseconds=10),
                    bid=mid-.5,ask=mid+.5,index_value=np.nan,is_trial=False,is_continuous=True,source='synthetic_fixture'))
    return pd.DataFrame(quotes),pd.DataFrame(checkpoints),decisions


def build(quotes,checkpoints,tickers,decisions):
    assert importlib.util.find_spec('twquant.intraday_book_frame') is not None, 'Causal local-book frame builder is missing'
    from twquant.intraday_book_frame import build_intraday_book_frame
    return build_intraday_book_frame(quotes,checkpoints,tickers,expected_decisions=decisions,horizon_seconds=30)


def test_late_revisions_and_future_prices_never_enter_known_book_features():
    q,c,d=inputs()
    a=build(q,c,['2330'],d)
    late=q.iloc[[20]].copy();late['received_at']=d[0]+pd.Timedelta(seconds=1)
    late['bid_sizes']=[[99999]*5];late['ask_sizes']=[[1]*5]
    changed=c.copy();changed['ask']+=999
    b=build(pd.concat([q,late],ignore_index=True),changed,['2330'],d)
    pd.testing.assert_frame_equal(a.filter(like='f_book_'),b.filter(like='f_book_'))
    assert not a.actual_return.equals(b.actual_return)
    assert a.input_ready.all()
    assert a.max_input_available_at.le(a.feature_cutoff_at).all()
    assert a.feature_cutoff_at.eq(a.decision_at-pd.Timedelta(seconds=1)).all()
    assert a.label_available_at.gt(a.target_at).all()
    assert set(a.source)=={'synthetic_local_book_frame'}


def test_portable_features_are_invariant_to_price_and_quantity_units():
    q,c,d=inputs()
    a=build(q,c,['2330'],d)
    scaled=q.copy(deep=True)
    for column in ['bid_prices','ask_prices']:scaled[column]=scaled[column].map(lambda x:[10*v+200 for v in x])
    for column in ['bid_sizes','ask_sizes']:scaled[column]=scaled[column].map(lambda x:[100*v for v in x])
    b=build(scaled,c,['2330'],d)
    np.testing.assert_allclose(a.filter(like='f_book_'),b.filter(like='f_book_'),atol=1e-12)
    assert not any('log_depth' in column for column in a)
    assert 'f_book_relative_depth_5' in a


def test_invalid_selected_books_and_unknown_checkpoints_keep_the_complete_grid():
    q,c,d=inputs()
    q.loc[98,'is_trial']=True
    c=c[~c.price_at.eq(d[1]+pd.Timedelta(seconds=30))]
    a=build(q,c,['2330','1216'],d)
    assert len(a)==4
    assert not a[a.ticker.eq('1216')].input_ready.any()
    assert a[a.ticker.eq('1216')].actual_return.isna().all()
    first=a[a.ticker.eq('2330')].iloc[0]
    assert not first.input_ready and first.actual_return<0
    assert a[a.ticker.eq('2330')].iloc[1].actual_return!=a[a.ticker.eq('2330')].iloc[1].actual_return


def test_external_data_and_index_points_never_create_peer_or_index_book_features():
    q,c,d=inputs()
    a=build(q,c,['2330'],d)
    external_q,external_c,_=inputs(('1216',))
    index=external_c.copy();index['ticker']='IX0001';index['index_value']=(index.bid+index.ask)/2
    index[['bid','ask']]=np.nan
    b=build(pd.concat([q,external_q]),pd.concat([c,external_c,index]),['2330','1216','IX0001'],d)
    pd.testing.assert_frame_equal(a,b[b.ticker.eq('2330')].reset_index(drop=True))
    idx=b[b.ticker.eq('IX0001')]
    assert not idx.input_ready.any() and idx.filter(like='f_book_').isna().all().all()
    assert idx.actual_return.notna().all() and idx.price_basis.eq('index_points').all()


def test_builder_preserves_timezone_and_checkpoint_identity_contracts():
    q,c,d=inputs()
    naive=q.copy();naive['received_at']=naive.received_at.dt.tz_localize(None)
    with pytest.raises(ValueError):build(naive,c,['2330'],d)
    with pytest.raises(ValueError):build(q,pd.concat([c,c.iloc[[0]]]),['2330'],d)


def test_provider_names_or_input_claims_cannot_verify_local_quote_provenance():
    q,c,d=inputs()
    q['source']='fugle_local_archive';c['source']='fugle_local_archive'
    q['quote_provenance_verified']=True;c['evidence_kind']='historical'
    frame=build(q,c,['2330'],d)
    assert frame.evidence_kind.eq('unknown').all()
    assert not frame.quote_provenance_verified.any()
    assert frame.emission_clock_basis.eq('planned_research_cutoff').all()


def test_canonical_books_train_only_fixed_ten_and_use_the_same_model_for_all_fifty():
    from twquant.intraday_models import fit_intraday_model
    from twquant.direction_intraday import audit_intraday_predictions
    universe=(*TRAINING_TICKERS,*(str(4000+i) for i in range(40)))
    q,c,d=inputs(universe,days=3)
    frame=build(q,c,universe,d)
    training=frame[frame.ticker.isin(TRAINING_TICKERS)&frame.date.lt(frame.date.max())]
    model=fit_intraday_model(training,'history_logistic',fit_at=frame.decision_at.max()-pd.Timedelta(seconds=1))
    probe=frame[frame.date.eq(frame.date.max())].copy()
    p=model.predict_proba(probe)[:,1]
    probe=probe.assign(probability_up=p,decision_threshold=.5,model_version=model.version)
    audit=audit_intraday_predictions(probe,universe,expected_decisions=[d[-1]],horizon_seconds=30,evidence_kind='synthetic',min_dates=1)
    assert audit['prediction_coverage']==audit['answer_coverage']==1.
    assert not audit['real_world_target_verified']
    assert set(model.training_details['training_tickers'])==set(TRAINING_TICKERS)
