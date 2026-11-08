import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS

SYMBOLS=('xlf','xlp','xlc','xli','xlb','xle','smh')


def response_inputs(sessions=100):
    dates=pd.bdate_range('2024-01-02',periods=sessions)
    shocks=np.random.default_rng(31).normal(0.,.01,sessions)
    next_dates=pd.DatetimeIndex(pd.Series(dates).shift(-1))
    rows=[]
    for i,ticker in enumerate((*TRAINING_TICKERS,'0050','1216')):
        beta=.5+i*.08 if ticker!='1216' else -.8
        frame=pd.DataFrame(dict(date=dates,ticker=ticker,f_return_1=beta*pd.Series(shocks).shift(1).to_numpy(),target_1=.02))
        frame['decision_ts']=(next_dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=8,minutes=30)).tz_convert('UTC')
        for j,symbol in enumerate(SYMBOLS):frame[f'f_ext_us_sector_{symbol}_window_return']=shocks*(1+j*.1)
        rows.append(frame)
    return pd.concat(rows,ignore_index=True).sample(frac=1,random_state=6)


def test_sector_response_uses_previous_preopen_shock_and_own_known_returns():
    from twquant.sector_response import add_sector_response_features
    frame=response_inputs();out=add_sector_response_features(frame)
    pd.testing.assert_frame_equal(out[frame.columns],frame)
    dates=sorted(frame.date.unique());known=out.date.ge(dates[30])&out.date.lt(dates[-1])
    np.testing.assert_allclose(out.loc[known&out.ticker.eq('2330'),'f_response_xlf_beta60'],.5,rtol=1e-10)
    np.testing.assert_allclose(out.loc[known&out.ticker.eq('1216'),'f_response_xlf_beta60'],-.8,rtol=1e-10)
    expected=out.loc[known&out.ticker.eq('2330'),'f_ext_us_sector_xlf_window_return']*.5
    np.testing.assert_allclose(out.loc[known&out.ticker.eq('2330'),'f_response_xlf_beta_window_return'],expected,rtol=1e-10)
    cols=[c for c in out if c not in frame]
    assert len(cols)==14 and out.loc[out.date.lt(dates[30]),cols].isna().all().all()
    assert out.loc[out.decision_ts.isna(),cols].isna().all().all()
    assert out.attrs['sector_response_contract']['window_sessions']==60
    assert out.attrs['sector_response_contract']['minimum_pairs']==30
    assert out.attrs['sector_response_contract']['original_pit_verified'] is False


def test_current_shock_does_not_enter_its_own_beta_and_future_targets_are_irrelevant():
    from twquant.sector_response import add_sector_response_features
    frame=response_inputs();a=add_sector_response_features(frame);cut=sorted(frame.date.unique())[70]
    changed=frame.assign(target_1=-999.,next_open=999999.,next_high=888888.)
    changed.loc[changed.date.gt(cut),'f_return_1']*=100
    for symbol in SYMBOLS:changed.loc[changed.date.eq(cut),f'f_ext_us_sector_{symbol}_window_return']*=100
    b=add_sector_response_features(changed)
    beta=[c for c in a if c.endswith('_beta60')]
    pd.testing.assert_frame_equal(a.loc[frame.date.le(cut),beta],b.loc[frame.date.le(cut),beta])
    cols=[c for c in a if c.startswith('f_response_')]
    pd.testing.assert_frame_equal(a.loc[frame.date.lt(cut),cols],b.loc[frame.date.lt(cut),cols])
    known=frame.date.eq(cut)&frame.ticker.eq('2330')
    np.testing.assert_allclose(b.loc[known,'f_response_xlf_beta_window_return'],a.loc[known,'f_response_xlf_beta_window_return']*100)


def test_pairwise_missingness_clipping_and_external_isolation():
    from twquant.sector_response import add_sector_response_features
    frame=response_inputs();dates=sorted(frame.date.unique())
    frame.loc[frame.date.eq(dates[45]),'f_ext_us_sector_xlf_window_return']=np.nan
    frame.loc[frame.date.eq(dates[50])&frame.ticker.eq('2330'),'f_return_1']=np.nan
    frame.loc[frame.ticker.eq('2454'),'f_return_1']*=100
    a=add_sector_response_features(frame)
    later=a.date.ge(dates[60])&a.decision_ts.notna()
    np.testing.assert_allclose(a.loc[later&a.ticker.eq('2330'),'f_response_xlf_beta60'],.5,rtol=1e-10)
    assert a.loc[later&a.ticker.eq('2454'),'f_response_xlf_beta60'].eq(3.).all()
    changed=frame.copy();changed.loc[changed.ticker.eq('1216'),'f_return_1']*=100
    for symbol in SYMBOLS:changed.loc[changed.ticker.eq('1216'),f'f_ext_us_sector_{symbol}_window_return']=777.
    b=add_sector_response_features(changed)
    cols=[c for c in a if c.startswith('f_response_')]
    core=frame.ticker.isin(TRAINING_TICKERS)
    pd.testing.assert_frame_equal(a.loc[core,cols],b.loc[core,cols])


@pytest.mark.parametrize('problem',['missing_column','duplicate','nonuniform_context','naive','postclose','infinite'])
def test_sector_response_rejects_ambiguous_context_or_clock(problem):
    from twquant.sector_response import add_sector_response_features
    frame=response_inputs()
    if problem=='missing_column':frame=frame.drop(columns='f_ext_us_sector_xlf_window_return')
    elif problem=='duplicate':frame=pd.concat([frame,frame.iloc[[0]]])
    elif problem=='nonuniform_context':frame.loc[frame.loc[frame.ticker.eq('2330')].index[40],'f_ext_us_sector_xlf_window_return']=999.
    elif problem=='naive':frame['decision_ts']=frame.decision_ts.dt.tz_localize(None)
    elif problem=='postclose':frame['decision_ts']+=pd.Timedelta(hours=11,minutes=30)
    else:frame.loc[frame.index[0],'f_return_1']=np.inf
    with pytest.raises(ValueError):add_sector_response_features(frame)


def test_response_profile_isolated_from_all_original_profiles():
    from twquant.features import select_feature_columns
    columns=['f_return_1','f_path_up_1_60','f_flow_foreign_imbalance','f_margin_finance_utilization',
             'f_ext_us_sector_xlf_window_return','f_response_xlf_beta60','f_openref_log_gap','f_ext_us_sector_xlf_close','f_ext_fred_vix']
    frame=pd.DataFrame(columns=columns)
    for profile in ('portable_price','portable_market','portable_all','portable_path','portable_sector','portable_flow','portable_opening','portable_margin'):
        assert select_feature_columns(frame,profile)==select_feature_columns(frame.drop(columns='f_response_xlf_beta60'),profile)
    assert select_feature_columns(frame,'portable_response')==columns[:6]


def test_direction_search_requires_sector_response_contract():
    from test_direction import preopen_frame,search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    frame=preopen_frame(search_frame()).assign(f_response_xlf_beta60=.5)
    with pytest.raises(ValueError,match='Sector response.*contract'):
        search_direction_models(frame,small_config(horizons=(1,),max_folds=1),candidates=(DirectionCandidate('prior'),),holdout_dates=15,bootstrap_samples=10,mode='preopen')
