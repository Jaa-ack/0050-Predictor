"""Frozen-model temporal residual intervals; no classifier or point-model changes.

Darts motivates the calibration wrapper and exact finite-sample rank. Scaling
by known trailing volatility is this study's adaptation, not a Darts benchmark.
Dependent stocks and overlapping labels have no exchangeability guarantee.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ResidualInterval:
    horizon: int
    scaled: bool
    radius: float
    fallback: float
    floor: float
    calibration_rows: int

    def scales(self, frame):
        if not self.scaled:
            return np.ones(len(frame))
        volatility = pd.to_numeric(frame.f_volatility_20, errors='coerce').to_numpy(dtype=float)
        volatility = np.where(np.isfinite(volatility) & (volatility >= 0), volatility, self.fallback)
        return np.maximum(volatility, self.floor) * np.sqrt(self.horizon)

    def apply(self, frame, predictions):
        if len(frame) != len(predictions):
            raise ValueError('Interval frames must align with predictions')
        out = predictions.copy()
        center = np.log1p(out.expected_return.to_numpy(dtype=float))
        width = self.radius * self.scales(frame)
        out['q10'] = np.expm1(np.clip(center - width, -10, 10))
        out['q90'] = np.expm1(np.clip(center + width, -10, 10))
        out['q50'] = out.expected_return
        return out


def fit_interval(training, calibration, calibration_predictions, horizon, *, scaled):
    """Fit scales on training only, then rank mature calibration residuals.

    Caller supplies the existing maturity-purged split. No test rows or labels
    are accepted during fitting. All calibration stocks must be fixed-ten.
    """
    if horizon not in (1, 5, 20) or len(calibration) != len(calibration_predictions):
        raise ValueError('Invalid interval horizon or calibration alignment')
    from .universe import TRAINING_TICKERS
    for frame in (training, calibration):
        if 'ticker' in frame and not frame.ticker.isin(TRAINING_TICKERS).all():
            raise ValueError('Interval fitting requires fixed-ten stocks only')
    known = pd.to_numeric(training.f_volatility_20, errors='coerce').to_numpy(dtype=float)
    known = known[np.isfinite(known) & (known > 0)]
    fallback = float(np.median(known)) if len(known) else .01
    floor = max(1e-6, .1 * fallback)
    provisional = ResidualInterval(horizon, scaled, 0., fallback, floor, 0)
    center = np.log1p(calibration_predictions.expected_return.to_numpy(dtype=float))
    residual = np.abs(calibration[f'target_{horizon}'].to_numpy(dtype=float) - center)
    scores = residual / provisional.scales(calibration)
    scores = scores[np.isfinite(scores)]
    if not len(scores):
        raise ValueError('No finite calibration residuals')
    rank = min(len(scores), int(np.ceil((len(scores) + 1) * .8)))
    radius = float(np.partition(scores, rank - 1)[rank - 1])
    return ResidualInterval(horizon, scaled, radius, fallback, floor, len(scores))
