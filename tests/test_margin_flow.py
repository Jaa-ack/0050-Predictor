import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def margin_inputs():
    dates = pd.bdate_range('2024-01-02', periods=45)
    frames, rows = [], []
    for ticker in (*TRAINING_TICKERS, '0050', '1216', 'IX0001'):
        frame = pd.DataFrame(dict(date=dates, ticker=ticker, f_return_1=.01, target_1=.02))
        frame['decision_ts'] = (dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=21)).tz_convert('UTC')
        frames.append(frame)
        if ticker == 'IX0001': continue
        for date in dates:
            row = dict(date=date, stock_id=ticker, source='finmind_historical_margin_short', Note='', OffsetLoanAndShort=0)
            for prefix in ('MarginPurchase','ShortSale'):
                row.update({prefix+'Buy':30, prefix+'Sell':10, prefix+'CashRepayment':10,
                            prefix+'Limit':1000, prefix+'TodayBalance':300, prefix+'YesterdayBalance':200})
            rows.append(row)
    return pd.concat(frames,ignore_index=True).sample(frac=1,random_state=6), pd.DataFrame(rows)


def test_margin_features_use_own_reported_ratios_and_two_session21_receipt_lag():
    from twquant.margin_flow import add_margin_features
    frame,history = margin_inputs()
    out = add_margin_features(frame,history)
    pd.testing.assert_frame_equal(out[frame.columns],frame)
    dates = sorted(frame.date.unique())
    known = out.date.ge(dates[2]) & out.ticker.ne('IX0001')
    for column,value in [('f_margin_finance_imbalance',.5),('f_margin_short_imbalance',-.5),
                         ('f_margin_finance_utilization',.3),('f_margin_short_balance_change',.2),
                         ('f_margin_finance_repayment_share',.2),('f_margin_short_limit_positive',1.)]:
        np.testing.assert_allclose(out.loc[known,column],value)
    new = [c for c in out if c not in frame]
    assert len(new)==18 and out.loc[out.date.lt(dates[2]),new].isna().all().all()
    assert out.loc[out.ticker.eq('IX0001'),new].isna().all().all()
    assert out.loc[out.date.lt(dates[21]),'f_margin_finance_balance_change_mean20'].isna().all()
    assert out.attrs['margin_receipt_contract']['assumed_receipt'].startswith('21:00')
    assert out.attrs['margin_receipt_contract']['lag_sessions']==2
    assert out.attrs['margin_receipt_contract']['evidence_kind']=='historical'


def test_zero_activity_zero_limit_and_absent_rows_remain_distinct_without_stale_carry():
    from twquant.margin_flow import add_margin_features
    frame,history = margin_inputs();dates=sorted(frame.date.unique())
    quantities=[c for c in history if c.startswith(('MarginPurchase','ShortSale'))]
    history.loc[history.date.eq(dates[10]),quantities]=0
    history=history.loc[history.date.ne(dates[11])]
    out=add_margin_features(frame,history)
    zero=out.date.eq(dates[12]) & out.ticker.ne('IX0001')
    assert out.loc[zero,'f_margin_finance_imbalance'].eq(0).all()
    assert out.loc[zero,'f_margin_finance_balance_change'].eq(0).all()
    assert out.loc[zero,'f_margin_finance_limit_positive'].eq(0).all()
    assert out.loc[zero,'f_margin_finance_active'].eq(0).all()
    assert out.loc[zero,'f_margin_finance_utilization'].isna().all()
    assert out.loc[out.date.eq(dates[13]),[c for c in out if c.startswith('f_margin_')]].isna().all().all()
    assert out.loc[out.date.eq(dates[16]),'f_margin_finance_mean5'].isna().all()
    known=out.date.eq(dates[18]) & out.ticker.ne('IX0001')
    np.testing.assert_allclose(out.loc[known,'f_margin_finance_mean5'],.5)


def test_margin_ratios_are_scale_free_causal_and_isolated_from_external_stock_histories():
    from twquant.margin_flow import add_margin_features
    frame,history=margin_inputs();a=add_margin_features(frame,history)
    columns=[c for c in a if c.startswith('f_margin_')]
    cut=sorted(frame.date.unique())[25]
    changed=history.copy()
    changed.loc[changed.date.gt(cut)|changed.stock_id.eq('1216'),'MarginPurchaseBuy']*=100
    changed['target_1']=999.
    b=add_margin_features(frame.assign(target_1=-999.),changed)
    mask=frame.date.le(cut)&frame.ticker.isin(TRAINING_TICKERS)
    pd.testing.assert_frame_equal(a.loc[mask,columns],b.loc[mask,columns])
    quantities=[c for c in history if c.startswith(('MarginPurchase','ShortSale')) or c=='OffsetLoanAndShort']
    scaled=history.copy();scaled[quantities]*=1000
    pd.testing.assert_frame_equal(a,add_margin_features(frame,scaled))


def test_margin_receipt_expiry_and_calendar_cannot_be_extended_by_external_only_dates():
    from twquant.margin_flow import add_margin_features
    frame,history=margin_inputs();dates=sorted(frame.date.unique());date=dates[25]
    cutoff=(pd.Timestamp(dates[26]).tz_localize('Asia/Taipei')+pd.Timedelta(hours=8,minutes=30)).tz_convert('UTC')
    frame.loc[frame.date.eq(date),'decision_ts']=cutoff
    a=add_margin_features(frame,history)
    assert a.loc[a.date.eq(date)&a.ticker.eq('2330'),'f_margin_finance_imbalance'].iloc[0]==.5
    frame.loc[frame.date.eq(date),'decision_ts']=cutoff+pd.Timedelta(microseconds=1)
    b=add_margin_features(frame,history)
    cols=[c for c in a if c.startswith('f_margin_')]
    assert b.loc[b.date.eq(date),cols].isna().all().all()
    frame.loc[frame.date.eq(date),'decision_ts']=pd.NaT
    assert add_margin_features(frame,history).loc[frame.date.eq(date),cols].isna().all().all()
    extra=frame.iloc[[0]].assign(ticker='1216',date=pd.Timestamp('2024-02-03'),decision_ts=pd.Timestamp('2024-02-03T13:00:00Z'))
    extended=add_margin_features(pd.concat([frame,extra],ignore_index=True),history)
    pd.testing.assert_frame_equal(add_margin_features(frame,history)[cols].reset_index(drop=True),extended.iloc[:-1][cols])
    assert extended.iloc[-1][cols].isna().all()


@pytest.mark.parametrize('problem',['lag','duplicate','negative','nonfinite','source','naive','early','stock_id'])
def test_margin_rejects_invalid_histories_or_receipt_contract(problem):
    from twquant.margin_flow import add_margin_features
    frame,history=margin_inputs();lag=2
    if problem=='lag':lag=True
    elif problem=='duplicate':history=pd.concat([history,history.iloc[[0]]])
    elif problem=='negative':history.loc[0,'MarginPurchaseBuy']=-1.
    elif problem=='nonfinite':
        history['ShortSaleLimit']=history.ShortSaleLimit.astype(float)
        history.loc[0,'ShortSaleLimit']=np.inf
    elif problem=='source':history['source']='unverified_vendor'
    elif problem=='naive':frame['decision_ts']=frame.decision_ts.dt.tz_localize(None)
    elif problem=='early':frame['decision_ts']-=pd.Timedelta(seconds=1)
    else:history['stock_id']=2330
    with pytest.raises(ValueError):add_margin_features(frame,history,lag_sessions=lag)


def test_margin_profile_preserves_all_previous_portable_profiles_and_excludes_opening_proxies():
    from twquant.features import select_feature_columns
    columns=['f_return_1','f_path_up_1_60','f_flow_foreign_imbalance','f_ext_us_sector_xlf_return1',
             'f_margin_finance_imbalance','f_openref_log_gap','f_ext_fred_vix','f_ext_fmp_spy_close']
    frame=pd.DataFrame(columns=columns)
    for profile in ('portable_price','portable_market','portable_all','portable_path','portable_sector','portable_flow','portable_opening'):
        assert select_feature_columns(frame,profile)==select_feature_columns(frame.drop(columns='f_margin_finance_imbalance'),profile)
    assert select_feature_columns(frame,'portable_margin')==columns[:5]


def test_direction_search_requires_margin_receipt_contract():
    from test_direction import preopen_frame,search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    frame=preopen_frame(search_frame()).assign(f_margin_finance_imbalance=.5)
    with pytest.raises(ValueError,match='Margin.*receipt contract'):
        search_direction_models(frame,small_config(horizons=(1,),max_folds=1),candidates=(DirectionCandidate('prior'),),holdout_dates=15,bootstrap_samples=10,mode='preopen')


def test_synthetic_margin_contract_forces_synthetic_research_and_versions_the_lag():
    from test_direction import preopen_frame,search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    from twquant.margin_flow import add_margin_features
    _,history=margin_inputs();history['source']='synthetic'
    frame=preopen_frame(search_frame());reports=[]
    for lag in (0,2):
        enriched=add_margin_features(frame,history,lag_sessions=lag)
        reports.append(search_direction_models(enriched,small_config(horizons=(1,),max_folds=1),
            candidates=(DirectionCandidate('prior'),),holdout_dates=15,bootstrap_samples=10,mode='preopen',evidence_kind='historical'))
    assert all(r.report['by_horizon']['1']['evidence_kind']=='synthetic' for r in reports)
    np.testing.assert_array_equal(reports[0].predictions.probability_up,reports[1].predictions.probability_up)
    assert set(reports[0].predictions.model_version).isdisjoint(reports[1].predictions.model_version)


def test_own_margin_ratios_never_enter_the_shared_market_factor_even_when_uniform():
    from test_direction_exposure import exposure_training_frame
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    frame=exposure_training_frame(noise=.01).assign(f_margin_finance_imbalance=.5)
    model,_=fit_direction_model(DirectionCandidate('factor_ridge'),frame,[c for c in frame if c.startswith('f_')],1,42)
    assert 'f_margin_finance_imbalance' not in model.training_details['market_features']
