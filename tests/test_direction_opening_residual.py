import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


NAMES = ('opening_residual_ridge', 'opening_residual_hist',
         'opening_residual_vol_ridge', 'opening_residual_vol_hist')


def training_frame():
    dates = pd.bdate_range('2023-01-02', periods=160)
    rng = np.random.default_rng(18)
    frame = pd.DataFrame([(d, t) for d in dates for t in TRAINING_TICKERS], columns=['date', 'ticker'])
    frame['f_openref_log_gap'] = rng.normal(0., .01, len(frame))
    frame['f_openref_present'] = 1.
    frame['f_signal'] = rng.normal(0., 1., len(frame))
    frame['f_volatility_20'] = .02
    frame['target_1'] = frame.f_openref_log_gap + .003*frame.f_signal + rng.normal(0., .002, len(frame))
    return frame


@pytest.mark.parametrize('name', NAMES)
def test_residual_model_combines_shared_future_return_distribution_with_own_reference(name):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    frame = training_frame()
    columns = [c for c in frame if c.startswith('f_')]
    model, used = fit_direction_model(DirectionCandidate(name, 'portable_opening', 100), frame, columns, 1, 42)
    probe = pd.DataFrame(dict(ticker='1216', f_openref_log_gap=[-.02, .02, np.nan],
                              f_openref_present=[1.,1.,0.], f_signal=0., f_volatility_20=.02))
    probabilities = model.predict_proba(probe)
    assert probabilities.shape == (3,2) and np.isfinite(probabilities).all()
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.)
    assert probabilities[0,1] < .05 and probabilities[1,1] > .95
    assert .2 < probabilities[2,1] < .8
    assert used.date.nunique() == 100 and set(used.ticker)==set(TRAINING_TICKERS)
    details = model.training_details
    assert details['method']=='assumed_opening_gap_plus_shared_remaining_log_return'
    assert details['gap_missing_fallback']==0.
    assert details['residual_training_rows']==len(used)
    assert details['probability_scale'] > 0
    # Predictions never learn new parameters from another inference batch or future fields.
    model.predict_proba(probe.assign(f_openref_log_gap=99., f_signal=99., target_1=999.))
    np.testing.assert_array_equal(probabilities, model.predict_proba(probe.assign(target_1=-999.)))


@pytest.mark.parametrize('name', NAMES)
def test_missing_opening_reference_is_declared_unavailable_and_external_training_is_rejected(name):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import DirectionCandidateUnavailable, fit_direction_model
    frame=training_frame();columns=[c for c in frame if c.startswith('f_')]
    candidate=DirectionCandidate(name,'portable_opening')
    with pytest.raises(DirectionCandidateUnavailable) as exc:
        fit_direction_model(candidate,frame.assign(f_openref_log_gap=np.nan),columns,1,42)
    assert exc.value.code=='opening_reference_training_missing'
    with pytest.raises(ValueError,match='fixed ten'):
        fit_direction_model(candidate,frame.assign(ticker='1216'),columns,1,42)


@pytest.mark.parametrize('name', NAMES[2:])
def test_residual_volatility_and_missing_defaults_come_only_from_training(name):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import DirectionCandidateUnavailable, fit_direction_model
    train=training_frame();columns=[c for c in train if c.startswith('f_')]
    candidate=DirectionCandidate(name,'portable_opening')
    model,_=fit_direction_model(candidate,train,columns,1,42)
    probe=pd.DataFrame(dict(f_openref_log_gap=.002, f_openref_present=1., f_signal=0.,
                            f_volatility_20=[.01,.02,.04,np.nan,0.,np.inf]))
    p=model.predict_proba(probe)[:,1]
    assert .5 < p[2] < p[1] < p[0] < 1.
    np.testing.assert_allclose(p[3:],p[1])
    assert model.training_details['volatility_default']==pytest.approx(.02)
    assert model.training_details['volatility_floor']==pytest.approx(.002)
    with pytest.raises(DirectionCandidateUnavailable) as exc:
        fit_direction_model(candidate,train.assign(f_volatility_20=0.),columns,1,42)
    assert exc.value.code=='decision_time_volatility_missing'
