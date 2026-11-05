import numpy as np
import pandas as pd
import pytest

from test_research import research_frame,small_config
from test_fixed_universe import expanded_prices
from twquant.features import build_features,get_feature_columns
from twquant.research import run_research,_horizon_metrics,simulate_portfolio


def test_metrics_expose_one_sided_direction_and_constant_probability_baselines():
    rows=pd.DataFrame({'ticker':['2330']*4,'date':pd.date_range('2024-01-01',periods=4),
        'fold':[1]*4,'actual_return':[.1,.1,-.1,-.1],'probability_up':[.8]*4,
        'baseline_probability':[.8]*4,'training_probability':[.6]*4,
        'expected_return':[.01]*4,'baseline_return':[0.]*4,'q10':[-.2]*4,'q90':[.2]*4})
    metrics=_horizon_metrics(rows)
    assert metrics['predicted_up_fraction']==1
    assert metrics['balanced_accuracy']==.5
    assert metrics['recall_down']==0
    assert metrics['fixed_p50_brier']==.25
    assert metrics['frequency_brier']==pytest.approx(.26)
    assert metrics['brier_skill_vs_p50']<0
    assert metrics['reliability'][4]['predicted']==.8


def test_training_frequency_and_calibration_selection_never_use_test_labels():
    cfg=small_config(horizons=(1,),max_folds=1,calibration_policy='chronological_shrinkage')
    frame=research_frame();a=run_research(frame,['f_signal'],cfg)
    changed=frame.copy();dates=a.predictions.date.unique()
    changed.loc[changed.date.isin(dates),'target_1']=.9
    b=run_research(changed,['f_signal'],cfg)
    np.testing.assert_allclose(a.predictions.training_probability,b.predictions.training_probability)
    np.testing.assert_allclose(a.predictions.probability_up,b.predictions.probability_up)
    model=a.models[1]
    assert 0<=model.calibration_prior_weight<=1
    assert pd.Timestamp(model.calibration_selection['fit_label_end_max'])<pd.Timestamp(model.calibration_selection['validation_start'])
    assert np.isfinite(a.predictions.arithmetic_expected_return).all()


def test_execution_objective_uses_open_to_close_labels_instead_of_close_labels():
    _,prices=expanded_prices();frame=build_features(prices)
    cfg=small_config(horizons=(1,),max_folds=1,objective='open_return')
    a=run_research(frame,get_feature_columns(frame),cfg)
    changed=frame.copy();changed['target_1']=.9
    b=run_research(changed,get_feature_columns(changed),cfg)
    assert a.models[1].model_version==b.models[1].model_version
    assert a.metrics['objective']=='open_return'


def test_trading_selection_uses_arithmetic_estimate_and_reports_same_trade_gross():
    rows=pd.DataFrame({'ticker':['2330','2454'],'date':pd.to_datetime(['2024-01-01']*2),
        'next_open_date':pd.to_datetime(['2024-01-02']*2),'horizon':[1]*2,'probability_up':[.8]*2,
        'expected_return':[.3,.2],'arithmetic_expected_return':[-.1,.1],
        'trade_return_1':[-.5,.1],'benchmark_trade_return_1':[.01]*2,'benchmark_target_1':[.01]*2,
        'next_open':[100.]*2,'trade_valid_1':[True]*2})
    equity,metrics=simulate_portfolio(rows,small_config(max_positions=1,max_weight=1.))
    assert metrics['orders']==2
    assert metrics['gross_strategy']['total_return']==pytest.approx(.1)
    assert metrics['strategy']['total_return']<metrics['gross_strategy']['total_return']


def test_profile_comparison_is_unchanged_when_reserved_outcomes_change():
    from twquant.comparison import compare_profiles
    from twquant.demo import make_demo_prices
    p=make_demo_prices(150)
    cfg=small_config(horizons=(1,),max_folds=1,calibration_policy='chronological_shrinkage')
    a=compare_profiles(p,cfg,holdout_dates=15,profiles=('portable_price','portable_all'))
    changed=p.copy();start=sorted(p.date.unique())[-15]
    mask=changed.date>=start
    changed.loc[mask,['open','high','low','close','adj_close']]*=2
    b=compare_profiles(changed,cfg,holdout_dates=15,profiles=('portable_price','portable_all'))
    assert a['selected_profile']==b['selected_profile']
    assert a['scores']==b['scores']
    assert a['holdout_opened'] is False


def test_rolling_training_window_reports_only_sessions_actually_fitted():
    cfg=small_config(horizons=(1,),max_folds=1,min_train_dates=20,training_window_dates=30)
    result=run_research(research_frame(),['f_signal'],cfg)
    assert result.folds.train_dates.max()<=30
    assert result.folds.train_rows.max()<=30*3


def test_execution_outcomes_cannot_be_admitted_as_features():
    frame=research_frame();frame['execution_target_1']=frame.target_1
    with pytest.raises(ValueError,match='feature columns'):
        run_research(frame,['execution_target_1'],small_config(horizons=(1,),max_folds=1))


def test_reliability_boundary_probability_is_counted_exactly_once():
    rows=pd.DataFrame({'date':pd.date_range('2024-01-01',periods=2),'ticker':'2330','fold':1,
        'actual_return':[.1,-.1],'probability_up':[.6,.6],'baseline_probability':.5,
        'training_probability':.5,'expected_return':0.,'baseline_return':0.,'q10':-.2,'q90':.2})
    metrics=_horizon_metrics(rows)
    assert sum(b['count'] for b in metrics['reliability'])==2
