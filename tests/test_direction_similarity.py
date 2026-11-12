import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def fit_case():
    dates=pd.bdate_range('2020-01-02',periods=80)
    rows=[]
    for ticker in TRAINING_TICKERS:
        signal=np.linspace(-2.,2.,len(dates))
        rows.append(pd.DataFrame(dict(ticker=ticker,date=dates,f_signal=signal,
            target_1=np.where(signal>0,.01,-.01))))
    return pd.concat(rows,ignore_index=True)


def test_shared_similarity_learns_from_known_states_and_keeps_outlier_fallback():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();candidate=DirectionCandidate('similarity_local',window_dates=60)
    model,used=fit_direction_model(candidate,train,['f_signal'],1,42)
    probability=model.predict_proba(pd.DataFrame(dict(f_signal=[-1.,1.,100.,np.nan])))[:,1]
    assert probability[0]<.1 and probability[1]>.9
    assert probability[2]==pytest.approx(used.target_1.gt(0).mean())
    assert np.isfinite(probability).all() and ((probability>=0)&(probability<=1)).all()
    assert used.date.nunique()==60 and set(used.ticker)==set(TRAINING_TICKERS)
    assert model.training_details['fallback']=='training_class_prior'
    assert model.training_details['minimum_neighbors']==20


def test_similarity_rejects_external_training_and_does_not_use_query_targets():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();candidate=DirectionCandidate('similarity_local')
    model,_=fit_direction_model(candidate,train,['f_signal'],1,42)
    query=pd.DataFrame(dict(f_signal=[-1.,1.],target_1=[999.,-999.],ticker=['1216','2002']))
    np.testing.assert_array_equal(model.predict_proba(query),model.predict_proba(query.assign(target_1=0.,ticker='0050')))
    with pytest.raises(ValueError,match='fixed ten'):
        fit_direction_model(candidate,train.assign(ticker='1216'),['f_signal'],1,42)


def test_similarity_uses_training_only_transforms_and_preserves_missing_distance():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();train['f_missing']=np.nan
    model,_=fit_direction_model(DirectionCandidate('similarity_wide'),train,['f_signal','f_missing'],1,42)
    probe=pd.DataFrame(dict(f_signal=[-.8,.8],f_missing=[np.nan,np.nan]))
    before=model.predict_proba(probe)
    model.predict_proba(pd.DataFrame(dict(f_signal=[1e12],f_missing=[1e12])))
    np.testing.assert_array_equal(before,model.predict_proba(probe))
    assert model.training_details['features']==['f_signal','f_missing']
    assert model.training_details['fitted_rows']==len(train)


def test_similarity_with_no_feature_information_keeps_a_finite_daily_prior():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();train['f_signal']=np.nan
    model,used=fit_direction_model(DirectionCandidate('similarity_tight'),train,['f_signal'],1,42)
    probability=model.predict_proba(pd.DataFrame(dict(f_signal=[np.nan,999.])))[:,1]
    np.testing.assert_allclose(probability,used.target_1.gt(0).mean())


def test_many_neighbors_from_too_few_dates_do_not_create_artificial_support():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();dates=sorted(train.date.unique())
    train['f_signal']=np.where(train.date.isin(dates[:2]),0.,100.)
    train.loc[train.date.isin(dates[:2]),'target_1']=.01
    model,used=fit_direction_model(DirectionCandidate('similarity_tight'),train,['f_signal'],1,42)
    probability=model.predict_proba(pd.DataFrame(dict(f_signal=[0.])))[:,1]
    np.testing.assert_allclose(probability,used.target_1.gt(0).mean())
    assert model.training_details['minimum_neighbor_dates']==5


def test_equal_distance_boundary_ties_do_not_select_arbitrary_chronological_labels():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train=fit_case();dates=sorted(train.date.unique())
    train['f_signal']=train.date.isin(dates[40:]).astype(float)
    positive=dates[:20]+dates[60:]
    train['target_1']=np.where(train.date.isin(positive),.01,-.01)
    model,_=fit_direction_model(DirectionCandidate('similarity_local'),train,['f_signal'],1,42)
    probe=pd.DataFrame(dict(f_signal=[0.,1.]))
    np.testing.assert_allclose(model.predict_proba(probe)[:,1],[.5,.5])
    np.testing.assert_array_equal(model.predict_proba(probe),np.vstack([model.predict_proba(probe.iloc[[i]]) for i in range(2)]))
