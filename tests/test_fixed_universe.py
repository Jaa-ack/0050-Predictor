import numpy as np
import pandas as pd

from twquant.demo import make_demo_prices
from twquant.features import build_features,get_feature_columns
from twquant.research import ResearchConfig,run_research


def expanded_prices(sessions=150):
    prices=make_demo_prices(sessions)
    extras=[]
    for index,ticker in enumerate(['1216','2002','2412','IX0001']):
        copy=prices[prices.ticker=='2330'].copy()
        copy['ticker']=ticker
        copy[['open','high','low','close','adj_close']]*=(index+2)
        copy['volume']*=10**index
        copy['asset_class']='index' if ticker=='IX0001' else 'equity'
        extras.append(copy)
    return prices,pd.concat([prices,*extras],ignore_index=True)


def config(**kwargs):
    return ResearchConfig(horizons=(1,),min_train_dates=30,calibration_dates=10,
        test_dates=10,max_folds=1,**kwargs)


def test_test_assets_do_not_change_fixed_ten_features_or_models():
    core,expanded=expanded_prices()
    a=build_features(core);b=build_features(expanded)
    cols=get_feature_columns(a)
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True),
        b[b.ticker.isin(core.ticker.unique())][cols].reset_index(drop=True))
    first=run_research(a,cols,config());second=run_research(b,cols,config())
    assert first.models[1].model_version==second.models[1].model_version
    assert set(second.predictions.ticker)==set(core.ticker)-{'0050'}


def test_transfer_scoring_uses_same_frozen_core_model_and_dates():
    _,prices=expanded_prices();frame=build_features(prices)
    cfg=config(evaluation_tickers=('0050','1216','2002','2412','IX0001'))
    result=run_research(frame,get_feature_columns(frame),cfg)
    transfer=result.transfer_predictions
    assert set(transfer.ticker)==set(cfg.evaluation_tickers)
    assert set(transfer.date)==set(result.predictions.date)
    assert transfer.model_version.nunique()==1
    assert transfer.model_version.iloc[0]==result.models[1].model_version
    assert result.metrics['transfer']['IX0001']['tradable'] is False
    changed=frame.copy()
    changed.loc[changed.ticker.isin(cfg.evaluation_tickers),'target_1']=.99
    other=run_research(changed,get_feature_columns(changed),cfg)
    assert other.models[1].model_version==result.models[1].model_version
    np.testing.assert_allclose(other.transfer_predictions.probability_up,transfer.probability_up)


def test_holdout_also_scores_unseen_assets_without_fitting_on_them():
    from twquant.holdout import evaluate_holdout
    _,prices=expanded_prices();frame=build_features(prices)
    cfg=config(evaluation_tickers=('0050','1216','2002','2412','IX0001'))
    a=evaluate_holdout(frame,get_feature_columns(frame),cfg,holdout_dates=15)
    assert set(a.transfer_predictions.ticker)==set(cfg.evaluation_tickers)
    assert set(a.transfer_predictions.date)==set(a.predictions.date)
    assert a.metrics['transfer']['IX0001']['tradable'] is False
    changed=frame.copy();changed.loc[changed.ticker.isin(cfg.evaluation_tickers),'target_1']=.8
    b=evaluate_holdout(changed,get_feature_columns(frame),cfg,holdout_dates=15)
    assert a.models[1].model_version==b.models[1].model_version
    np.testing.assert_allclose(a.transfer_predictions.probability_up,b.transfer_predictions.probability_up)


def test_workflow_persists_transfer_and_accepts_explicit_paper_targets(tmp_path):
    from twquant.data import PriceStore
    from twquant.workflows import research,release_holdout,forecast
    _,prices=expanded_prices();PriceStore(tmp_path/'data').write_prices(prices)
    cfg=config(evaluation_tickers=('0050','1216','2002','2412','IX0001'),feature_profile='portable_price')
    cutoff=pd.to_datetime(prices.available_at,utc=True).max()
    report=research(tmp_path,cfg,cutoff,holdout_dates=15)
    folder=tmp_path/'runs'/report['run_id']
    assert (folder/'transfer_oos_predictions.parquet').exists()
    holdout=release_holdout(tmp_path)
    assert set(holdout['metrics']['transfer'])==set(cfg.evaluation_tickers)
    assert (folder/'transfer_holdout_predictions.parquet').exists()
    result=forecast(tmp_path,cutoff,paper=True,tickers=['0050','1216','IX0001'])
    assert result['total']==3
    assert {r['evaluation_role'] for r in result['forecasts']}=={'benchmark_transfer','external_stock_transfer','market_transfer'}
    index=next(r for r in result['forecasts'] if r['ticker']=='IX0001')
    assert index['tradable'] is False
    assert index['valuation']['status']=='not_applicable'


def test_unknown_index_volume_survives_features_and_transfer_scoring():
    _,prices=expanded_prices()
    row=prices.index[prices.ticker.eq('IX0001')][70]
    prices.loc[row,'volume']=np.nan
    frame=build_features(prices)
    assert frame.loc[frame.ticker.eq('IX0001'),'volume'].isna().sum()==1
    result=run_research(frame,get_feature_columns(frame),config(evaluation_tickers=('IX0001',)))
    assert not result.transfer_predictions.empty


def test_0050_is_only_evaluated_and_cannot_change_training_market_context():
    from twquant.universe import TRAINING_TICKERS
    prices,_=expanded_prices();changed=prices.copy()
    mask=changed.ticker.eq('0050')
    multiplier=np.exp(np.linspace(0,1,mask.sum()))
    for column in ['open','high','low','close','adj_close']:
        changed.loc[mask,column]*=multiplier
    a=build_features(prices);b=build_features(changed);columns=get_feature_columns(a)
    pd.testing.assert_frame_equal(a.loc[a.ticker.isin(TRAINING_TICKERS),columns].reset_index(drop=True),
        b.loc[b.ticker.isin(TRAINING_TICKERS),columns].reset_index(drop=True))


def test_transfer_forecast_requires_complete_fixed_ten_context(tmp_path):
    import pytest
    from twquant.data import PriceStore
    from twquant.workflows import research,forecast
    _,prices=expanded_prices();store=PriceStore(tmp_path/'data');store.write_prices(prices)
    cutoff=pd.to_datetime(prices.available_at,utc=True).max()
    research(tmp_path,config(evaluation_tickers=('0050',)),cutoff,holdout_dates=15)
    store.write_prices(prices.loc[~(prices.ticker.eq('2330') & prices.date.eq(prices.date.max()))])
    with pytest.raises(ValueError,match='training.*context'):
        forecast(tmp_path,cutoff,paper=True,tickers=['0050'])
