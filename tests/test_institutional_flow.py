import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def flow_inputs():
    dates = pd.bdate_range('2024-01-02',periods=45)
    frames = []
    for ticker in (*TRAINING_TICKERS,'0050','1216','IX0001'):
        f = pd.DataFrame(dict(date=dates,ticker=ticker,f_return_1=.01,target_1=.02))
        f['decision_ts'] = (dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=20)).tz_convert('UTC')
        frames.append(f)
    frame = pd.concat(frames,ignore_index=True).sample(frac=1,random_state=6)
    rows = []
    for ticker in (*TRAINING_TICKERS,'0050','1216'):
        for i,date in enumerate(dates):
            rows.extend([dict(date=date,stock_id=ticker,name='Foreign_Investor',buy=3*i+3,sell=i+1,
                              source='finmind_historical_institutional'),
                         dict(date=date,stock_id=ticker,name='Investment_Trust',buy=i+1,sell=3*i+3,
                              source='finmind_historical_institutional')])
    return frame,pd.DataFrame(rows)


def test_own_flow_uses_declared_two_session_lag_and_preserves_original_rows():
    from twquant.institutional_flow import add_institutional_flow_features
    frame,history = flow_inputs()
    out = add_institutional_flow_features(frame,history)
    pd.testing.assert_frame_equal(out[frame.columns],frame)
    dates = sorted(frame.date.unique())
    assert out.loc[out.date.lt(dates[2]),'f_flow_foreign_imbalance'].isna().all()
    known = out.date.ge(dates[2]) & out.ticker.ne('IX0001')
    np.testing.assert_allclose(out.loc[known,'f_flow_foreign_imbalance'],.5)
    np.testing.assert_allclose(out.loc[known,'f_flow_trust_imbalance'],-.5)
    assert out.loc[out.date.lt(dates[6]),'f_flow_foreign_mean5'].isna().all()
    assert out.loc[out.date.lt(dates[21]),'f_flow_foreign_mean20'].isna().all()
    new = [c for c in out if c not in frame]
    assert len(new)==8 and out.loc[out.ticker.eq('IX0001'),new].isna().all().all()
    assert out.attrs['flow_receipt_contract']['lag_sessions']==2
    assert out.attrs['flow_receipt_contract']['original_pit_verified'] is False
    same_day = add_institutional_flow_features(frame,history,lag_sessions=0)
    assert same_day.loc[same_day.date.eq(dates[0]) & same_day.ticker.eq('2330'),'f_flow_foreign_imbalance'].iloc[0]==.5


def test_explicit_zero_is_neutral_missing_reports_never_carry_and_rolling_windows_keep_gaps():
    from twquant.institutional_flow import add_institutional_flow_features
    frame,history = flow_inputs()
    dates = sorted(frame.date.unique())
    zero = history.date.eq(dates[10]) & history.name.eq('Foreign_Investor')
    history.loc[zero,['buy','sell']] = 0
    history = history.loc[~(history.date.eq(dates[11]) & history.name.eq('Foreign_Investor'))]
    out = add_institutional_flow_features(frame,history)
    zero_day = out.date.eq(dates[12]) & out.ticker.ne('IX0001')
    assert out.loc[zero_day,'f_flow_foreign_imbalance'].eq(0).all()
    assert out.loc[zero_day,'f_flow_foreign_active'].eq(0).all()
    missing_day = out.date.eq(dates[13])
    assert out.loc[missing_day,[c for c in out if c.startswith('f_flow_foreign_')]].isna().all().all()
    assert out.loc[out.date.eq(dates[16]),'f_flow_foreign_mean5'].isna().all()
    known = out.date.eq(dates[18]) & out.ticker.ne('IX0001')
    np.testing.assert_allclose(out.loc[known,'f_flow_foreign_mean5'],.5)


def test_flow_is_causal_scale_free_and_isolated_from_external_histories_and_legacy_dealers():
    from twquant.institutional_flow import add_institutional_flow_features
    frame,history = flow_inputs()
    a = add_institutional_flow_features(frame,history)
    cut = sorted(frame.date.unique())[25]
    changed = history.copy()
    changed.loc[changed.date.gt(cut) | changed.stock_id.eq('1216'),'buy'] *= 100
    changed['target_1'] = -500.
    b = add_institutional_flow_features(frame.assign(target_1=900.),changed)
    new = [c for c in a if c not in frame]
    mask = frame.date.le(cut) & frame.ticker.isin(TRAINING_TICKERS)
    pd.testing.assert_frame_equal(a.loc[mask,new],b.loc[mask,new])
    scaled = add_institutional_flow_features(frame,history.assign(buy=history.buy*1000,sell=history.sell*1000))
    pd.testing.assert_frame_equal(a,scaled)
    dealer = history.assign(name='Dealer',buy=999999.).drop_duplicates(['date','stock_id','name'])
    pd.testing.assert_frame_equal(a,add_institutional_flow_features(frame,pd.concat([history,dealer])))


def test_receipt_and_expiry_boundaries_terminal_unknown_and_external_calendar_isolation():
    from twquant.institutional_flow import add_institutional_flow_features
    frame,history = flow_inputs()
    date = sorted(frame.date.unique())[25]
    one = frame.loc[frame.date.eq(date)].copy()
    next_date = sorted(frame.date.unique())[26]
    cutoff = (pd.Timestamp(next_date).tz_localize('Asia/Taipei')+pd.Timedelta(hours=8,minutes=30)).tz_convert('UTC')
    all_dates = frame.copy()
    all_dates.loc[all_dates.date.eq(date),'decision_ts'] = cutoff
    a = add_institutional_flow_features(all_dates,history)
    assert a.loc[a.date.eq(date) & a.ticker.eq('2330'),'f_flow_foreign_imbalance'].iloc[0]==.5
    all_dates.loc[all_dates.date.eq(date),'decision_ts'] = cutoff+pd.Timedelta(microseconds=1)
    b = add_institutional_flow_features(all_dates,history)
    assert b.loc[b.date.eq(date),[c for c in b if c.startswith('f_flow_')]].isna().all().all()
    all_dates.loc[all_dates.date.eq(date),'decision_ts'] = pd.NaT
    assert add_institutional_flow_features(all_dates,history).loc[all_dates.date.eq(date),'f_flow_foreign_active'].isna().all()
    extra = one.loc[one.ticker.eq('1216')].assign(date=pd.Timestamp('2024-02-03'),decision_ts=pd.Timestamp('2024-02-04T00:30:00Z'))
    original = add_institutional_flow_features(frame,history)
    extended = add_institutional_flow_features(pd.concat([frame,extra],ignore_index=True),history)
    cols = [c for c in original if c.startswith('f_flow_')]
    pd.testing.assert_frame_equal(original[cols].reset_index(drop=True),extended.iloc[:-1][cols])
    assert extended.iloc[-1][cols].isna().all()


@pytest.mark.parametrize('problem',['negative_lag','duplicate','nonpositive_quantity','source','naive','early'])
def test_flow_rejects_invalid_inputs(problem):
    from twquant.institutional_flow import add_institutional_flow_features
    frame,history = flow_inputs();lag=2
    if problem=='negative_lag': lag=-1
    elif problem=='duplicate': history=pd.concat([history,history.iloc[[0]]])
    elif problem=='nonpositive_quantity': history.loc[0,'buy']=-1.
    elif problem=='source': history['source']='unverified_vendor'
    elif problem=='naive': frame['decision_ts']=frame.decision_ts.dt.tz_localize(None)
    else: frame['decision_ts']-=pd.Timedelta(seconds=1)
    with pytest.raises(ValueError): add_institutional_flow_features(frame,history,lag_sessions=lag)


def test_new_flow_profile_preserves_all_original_profiles_and_excludes_absolute_context():
    from twquant.features import select_feature_columns
    columns=['f_return_1','f_path_up_1_60','f_ext_us_sector_xlf_return1','f_flow_foreign_imbalance',
             'f_ext_fmp_spy_return1','f_ext_taifex_tx_night_return','f_ext_fred_vix','f_ext_us_sector_xlf_close']
    frame=pd.DataFrame(columns=columns)
    for profile in ('portable_price','portable_market','portable_all','portable_path','portable_sector'):
        assert select_feature_columns(frame,profile)==select_feature_columns(frame.drop(columns='f_flow_foreign_imbalance'),profile)
    assert select_feature_columns(frame,'portable_flow')==columns[:6]


def test_direction_research_requires_the_flow_receipt_contract():
    from test_direction import search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    frame=search_frame().assign(f_flow_foreign_imbalance=.5)
    with pytest.raises(ValueError,match='receipt contract'):
        search_direction_models(frame,small_config(horizons=(1,),max_folds=1),
            candidates=(DirectionCandidate('prior'),),holdout_dates=15,bootstrap_samples=10)


def test_company_flow_never_enters_the_shared_market_factor_even_when_uniform():
    from test_direction_exposure import exposure_training_frame
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    frame=exposure_training_frame(noise=.01).assign(f_flow_foreign_imbalance=.5)
    model,_=fit_direction_model(DirectionCandidate('factor_ridge'),frame,[c for c in frame if c.startswith('f_')],1,42)
    assert 'f_flow_foreign_imbalance' not in model.training_details['market_features']
