import importlib.util

import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def simulate(tickers=TRAINING_TICKERS, **options):
    assert importlib.util.find_spec('twquant.intraday_queue') is not None, 'Causal queue-process simulation is missing'
    from twquant.intraday_queue import QueueScenario, simulate_queue_panel
    dates=options.pop('dates',pd.bdate_range('2020-01-02',periods=40))
    scenario=options.pop('scenario',QueueScenario('fixture',flow_imbalance=.8,flip_rate=.002))
    return simulate_queue_panel(tickers,dates,scenario,seed=17,future_seed=options.pop('future_seed',29),**options)


def test_future_innovations_do_not_change_observed_features_or_emission_clocks():
    a=simulate();b=simulate(future_seed=43)
    columns=[c for c in a if c.startswith('f_book_')]
    assert len(columns)>=20
    pd.testing.assert_frame_equal(a[columns],b[columns])
    pd.testing.assert_frame_equal(a[['decision_at','prediction_at','max_input_available_at','input_ready']],
                                  b[['decision_at','prediction_at','max_input_available_at','input_ready']])
    assert not a.actual_return.equals(b.actual_return)
    assert a.max_input_available_at.le(a.prediction_at).all()
    assert a.prediction_at.le(a.decision_at).all()
    assert a.target_at.eq(a.decision_at+pd.Timedelta(seconds=30)).all()
    assert a.label_available_at.gt(a.target_at).all()
    assert set(a.source)=={'synthetic_queue_process'}


def test_extra_external_targets_and_future_dates_never_change_primary_simulated_history():
    a=simulate()
    extra=simulate((*TRAINING_TICKERS,'0050','1216','IX0001'))
    pd.testing.assert_frame_equal(a,extra[extra.ticker.isin(TRAINING_TICKERS)].reset_index(drop=True))
    longer=simulate(dates=pd.bdate_range('2020-01-02',periods=45))
    pd.testing.assert_frame_equal(a,longer[longer.date.le(a.date.max())].reset_index(drop=True))
    index=extra[extra.ticker.eq('IX0001')]
    assert not index.input_ready.any()
    assert index.filter(like='f_book_').isna().all().all()
    assert index.actual_return.notna().all()


def test_horizons_have_the_same_known_history_and_outages_still_have_rows():
    from twquant.intraday_queue import QueueScenario
    a=simulate(horizon_seconds=1)
    b=simulate(horizon_seconds=60)
    pd.testing.assert_frame_equal(a.filter(like='f_book_'),b.filter(like='f_book_'))
    outage=simulate(scenario=QueueScenario('outage',input_outage_fraction=1.))
    assert len(outage)==400 and not outage.input_ready.any()
    assert outage.filter(like='f_book_').isna().all().all()
    assert outage.actual_return.notna().all()
    assert outage.groupby('date').input_ready.nunique().eq(1).all()


def test_queue_parameters_and_synthetic_calendar_are_explicit_and_bounded():
    from twquant.intraday_queue import QueueScenario
    for options in [dict(flow_imbalance=1.1),dict(flip_rate=-.1),dict(input_outage_fraction=np.nan)]:
        with pytest.raises(ValueError):QueueScenario('bad',**options)
    with pytest.raises(ValueError):simulate(horizon_seconds=2)
    with pytest.raises(ValueError):simulate(dates=pd.DatetimeIndex(['2020-01-02','2020-01-02']))


def fit_model(frame, name='history_logistic', **kwargs):
    assert importlib.util.find_spec('twquant.intraday_models') is not None, 'Shared intraday classifier is missing'
    from twquant.intraday_models import fit_intraday_model
    return fit_intraday_model(frame,name,**kwargs)


def test_shared_model_only_fits_fixed_ten_mature_labels_and_ignores_inference_answers():
    train=simulate()
    model=fit_model(train,fit_at=train.label_available_at.max()+pd.Timedelta(days=1))
    probe=train.tail(10)
    before=model.predict_proba(probe)
    changed=probe.assign(actual_return=999.,ticker='1216')
    np.testing.assert_array_equal(before,model.predict_proba(changed))
    assert np.isfinite(before).all() and ((before>=0)&(before<=1)).all()
    assert set(model.training_details['training_tickers'])==set(TRAINING_TICKERS)
    with pytest.raises(ValueError,match='fixed ten'):
        fit_model(train.assign(ticker='1216'),fit_at=train.label_available_at.max()+pd.Timedelta(days=1))
    with pytest.raises(ValueError,match='mature'):
        fit_model(train,fit_at=train.label_available_at.max())


@pytest.mark.parametrize('name',['prior','snapshot_logistic','history_logistic','history_hist'])
def test_every_model_keeps_daily_prior_fallback_without_inference_refitting(name):
    train=simulate()
    model=fit_model(train,name,fit_at=train.label_available_at.max()+pd.Timedelta(days=1))
    probe=train.tail(4).copy()
    ready=model.predict_proba(probe)
    missing=probe.copy();missing.loc[:,'input_ready']=False
    np.testing.assert_allclose(model.predict_proba(missing)[:,1],model.prior)
    model.predict_proba(probe.assign(**{c:1e12 for c in probe.filter(like='f_book_')}))
    np.testing.assert_array_equal(ready,model.predict_proba(probe))
    assert model.training_details['training_rows']==len(train)
    assert model.training_details['flat_training_rows']==int(train.actual_return.eq(0).sum())


def test_training_rejects_late_known_inputs_and_a_shifted_target_clock():
    frame=simulate()
    stamp=frame.label_available_at.max()+pd.Timedelta(days=1)
    late=frame.copy();late.loc[0,'max_input_available_at']=late.loc[0,'decision_at']+pd.Timedelta(seconds=1)
    with pytest.raises(ValueError,match='causal'):
        fit_model(late,fit_at=stamp)
    shifted=frame.copy();shifted.loc[0,'target_at']+=pd.Timedelta(seconds=1)
    with pytest.raises(ValueError,match='clock'):
        fit_model(shifted,fit_at=stamp)


def test_a_premature_receipt_stamp_cannot_make_a_future_endpoint_a_mature_training_label():
    frame=simulate()
    frame['label_available_at']=frame.decision_at-pd.Timedelta(seconds=1)
    with pytest.raises(ValueError,match='mature'):
        fit_model(frame,fit_at=frame.decision_at.max())
