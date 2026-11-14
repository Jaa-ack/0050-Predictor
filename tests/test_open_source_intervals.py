import numpy as np
import pandas as pd
import pytest


def test_residual_interval_uses_exact_order_statistic_and_frozen_volatility():
    from twquant.open_source_intervals import fit_interval
    train = pd.DataFrame({'f_volatility_20': [.1, .1, np.nan]})
    calibration = pd.DataFrame({'f_volatility_20': [.1, .2, .1, .2],
                                'target_1': [.1, .4, .3, .8]})
    center = pd.DataFrame({'expected_return': np.zeros(4)})
    interval = fit_interval(train, calibration, center, 1, scaled=True)
    # Four scores [1,2,3,4]; ceil((4+1)*.8) = 4, not an interpolated rank.
    assert interval.radius == pytest.approx(4)
    test = pd.DataFrame({'f_volatility_20': [.1, .3, np.nan, 0], 'target_1': [99]*4})
    prediction = pd.DataFrame({'expected_return': np.zeros(4), 'probability_up': [.6]*4})
    result = interval.apply(test, prediction)
    np.testing.assert_allclose(np.log1p(result.q90), [.4, 1.2, .4, .04])
    np.testing.assert_allclose(np.log1p(result.q10), [-.4, -1.2, -.4, -.04])
    pd.testing.assert_frame_equal(prediction, result[prediction.columns])
    assert interval.fallback == .1
    assert interval.calibration_rows == 4


def test_pooled_control_has_constant_width_and_rejects_no_calibration_answers():
    from twquant.open_source_intervals import fit_interval
    train = pd.DataFrame({'f_volatility_20': [.02]})
    cal = pd.DataFrame({'f_volatility_20': [.02]*5, 'target_1': [.01, .02, .03, .04, .05]})
    center = pd.DataFrame({'expected_return': np.zeros(5)})
    interval = fit_interval(train, cal, center, 1, scaled=False)
    result = interval.apply(pd.DataFrame({'f_volatility_20': [.01, .3]}),
                            pd.DataFrame({'expected_return': [0., 0.]}))
    assert result.q90.iloc[0] == result.q90.iloc[1] == pytest.approx(np.expm1(.05))
    cal.target_1 = np.nan
    with pytest.raises(ValueError, match='calibration'):
        fit_interval(train, cal, center, 1, scaled=True)
