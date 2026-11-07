import numpy as np
import pandas as pd
import pytest

from twquant.universe import TRAINING_TICKERS


def test_taifex_context_enters_the_shared_factor_only_when_broadcast_across_fixed_ten():
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    frame = exposure_training_frame(noise=.01)
    frame['f_ext_taifex_tx_night_return'] = frame.f_market_return_1
    frame['f_ext_taifex_tf_night_return'] = frame.f_beta_60
    columns = [c for c in frame if c.startswith('f_')]
    model, _ = fit_direction_model(DirectionCandidate('factor_ridge'), frame, columns, 1, 42)
    assert 'f_ext_taifex_tx_night_return' in model.training_details['market_features']
    assert 'f_ext_taifex_tf_night_return' not in model.training_details['market_features']


def exposure_training_frame(noise=0.):
    dates = pd.bdate_range('2023-01-02', periods=160)
    frame = pd.DataFrame([(d, t) for d in dates for t in TRAINING_TICKERS], columns=['date', 'ticker'])
    frame['f_market_return_1'] = np.repeat(np.resize([-1., 1.], len(dates)), 10)
    frame['f_beta_60'] = np.tile(np.linspace(.2, 1.8, 10), len(dates))
    frame['f_volatility_20'] = .02
    frame['target_1'] = .02 * frame.f_beta_60 * frame.f_market_return_1
    frame['target_1'] += np.random.default_rng(81).normal(0., noise, len(frame))
    return frame


@pytest.mark.parametrize('name', ['factor_beta_ridge', 'factor_beta_hist', 'factor_beta_vol_ridge', 'factor_beta_vol_hist'])
def test_exposure_factor_uses_known_beta_for_unseen_assets_and_preserves_missing_beta_coverage(name):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train = exposure_training_frame()
    columns = ['f_market_return_1', 'f_beta_60', 'f_volatility_20']
    model, used = fit_direction_model(DirectionCandidate(name), train, columns, 1, 42)
    # Training betas are positive. The same fitted model must reverse its
    # market contribution for an unseen asset with a negative known exposure.
    probe = pd.DataFrame(dict(ticker=['1216'] * 4, f_market_return_1=1.,
                              f_beta_60=[-1., 2., np.nan, 0.], f_volatility_20=.02))
    probabilities = model.predict_proba(probe)[:, 1]
    assert probabilities[0] < .1
    assert probabilities[1] > .9
    assert probabilities[2] > .9  # Explicit unit market-exposure fallback, no dropped forecast.
    assert probabilities[3] == pytest.approx(.5, abs=1e-7)
    assert len(used) == len(train)
    details = model.training_details
    assert details['method'] == 'fixed_ten_beta_projection_plus_shared_stock_residual'
    assert details['beta_clip'] == [-3., 3.]
    assert details['beta_missing_fallback'] == 1.
    assert details['market_training_dates'] == train.date.nunique()
    assert details['residual_training_rows'] == len(train)
    before = model.predict_proba(probe)
    model.predict_proba(probe.assign(f_beta_60=900., f_volatility_20=900.))
    np.testing.assert_array_equal(model.predict_proba(probe), before)


@pytest.mark.parametrize('name', ['factor_beta_vol_ridge', 'factor_beta_vol_hist'])
def test_volatility_scores_change_uncertainty_with_training_only_missing_value_fallback(name):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import fit_direction_model
    train = exposure_training_frame(noise=.03)
    columns = ['f_market_return_1', 'f_beta_60', 'f_volatility_20']
    model, _ = fit_direction_model(DirectionCandidate(name), train, columns, 1, 42)
    probe = pd.DataFrame(dict(f_market_return_1=1., f_beta_60=1.,
                              f_volatility_20=[.01, .02, .04, np.nan, 0., np.inf]))
    p = model.predict_proba(probe)[:, 1]
    assert .5 < p[2] < p[1] < p[0] < 1.
    np.testing.assert_allclose(p[3:], p[1])
    other_batch = probe.iloc[:2].assign(f_volatility_20=[.2, np.nan])
    assert model.predict_proba(other_batch)[1, 1] == pytest.approx(p[1])
    assert model.training_details['volatility_default'] == pytest.approx(.02)
    assert model.training_details['volatility_floor'] == pytest.approx(.002)
    assert model.training_details['probability_method'] == 'training_residual_volatility_scaled_gaussian_approximation'


@pytest.mark.parametrize('problem,code', [
    ('missing_beta', 'decision_time_beta_missing'),
    ('unknown_beta', 'decision_time_beta_missing'),
    ('zero_exposure', 'market_exposure_not_identifiable'),
    ('missing_volatility', 'decision_time_volatility_missing'),
])
def test_exposure_candidates_report_missing_or_unidentifiable_training_inputs(problem, code):
    from twquant.direction import DirectionCandidate
    from twquant.direction_models import DirectionCandidateUnavailable, fit_direction_model
    train = exposure_training_frame()
    if problem == 'missing_beta':
        train = train.drop(columns='f_beta_60')
    elif problem == 'unknown_beta':
        train['f_beta_60'] = np.nan
    elif problem == 'zero_exposure':
        train['f_beta_60'] = 0.
    else:
        train['f_volatility_20'] = 0.
    columns = [c for c in train if c.startswith('f_')]
    with pytest.raises(DirectionCandidateUnavailable) as exc:
        fit_direction_model(DirectionCandidate('factor_beta_vol_ridge'), train, columns, 1, 42)
    assert exc.value.code == code
