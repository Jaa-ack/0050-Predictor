"""Training-only adapters for shared direction classification and factor returns."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import ndtr
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, HistGradientBoostingRegressor, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .universe import TRAINING_TICKERS


class DirectionCandidateUnavailable(ValueError):
    """A declared candidate lacks its required historical inputs."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _numeric(frame, columns):
    return frame[columns].apply(pd.to_numeric, errors='coerce').replace([np.inf, -np.inf], np.nan)


def _pipeline(estimator):
    return make_pipeline(SimpleImputer(strategy='median', keep_empty_features=True), StandardScaler(), estimator)


def _classifier(candidate, seed, labels):
    if candidate.model == 'prior' or len(np.unique(labels)) < 2:
        estimator = DummyClassifier(strategy='prior')
    elif candidate.model == 'logistic':
        estimator = LogisticRegression(C=.1, max_iter=1500, random_state=seed)
    elif candidate.model == 'hist':
        estimator = HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=7,
            min_samples_leaf=40, learning_rate=.05, l2_regularization=10, random_state=seed)
    else:
        cls = ExtraTreesClassifier if candidate.model == 'extra_trees' else RandomForestClassifier
        estimator = cls(n_estimators=96, max_depth=8, min_samples_leaf=25,
                        max_features=.8, random_state=seed, n_jobs=1)
    return _pipeline(estimator)


@dataclass
class _FactorDirectionModel:
    market: object
    residual: object
    market_columns: list[str]
    columns: list[str]
    scale: float
    training_details: dict

    @property
    def classes_(self):
        return np.array([0, 1])

    def predict_proba(self, frame):
        center = self.market.predict(_numeric(frame, self.market_columns)) + self.residual.predict(_numeric(frame, self.columns))
        p = ndtr(center / self.scale)
        return np.column_stack([1 - p, p])


def fit_direction_model(candidate, train, columns, horizon, seed):
    """Fit only the supplied mature training rows and return rows actually used.

    Factor models learn one market target per complete fixed-ten date and one
    pooled residual target per stock. The two fitted return estimates are added
    at inference; a training-residual Gaussian approximation supplies a score,
    not a claim of calibrated or guaranteed probabilities.
    """
    if not set(train.ticker) <= set(TRAINING_TICKERS):
        raise ValueError('Direction models may train only on the fixed ten')
    if candidate.window_dates is not None:
        dates = pd.DatetimeIndex(train.date.unique()).sort_values()
        train = train.loc[train.date >= dates[-min(candidate.window_dates, len(dates))]]
    if not candidate.model.startswith('factor_'):
        labels = train[f'target_{horizon}'].gt(0).to_numpy(dtype=int)
        model = _classifier(candidate, seed, labels).fit(_numeric(train, columns), labels)
        return model, train
    complete = train.groupby('date').ticker.nunique().eq(len(TRAINING_TICKERS))
    train = train.loc[train.date.isin(complete[complete].index)].copy()
    if train.date.nunique() < 10:
        raise DirectionCandidateUnavailable('incomplete_fixed_ten_market_history')
    market_columns = [c for c in columns if c.startswith('f_market_')]
    # Global context must actually be broadcast equally across the fixed ten.
    # Company-specific or partially populated external features remain solely
    # in the stock residual fit.
    for c in columns:
        if c.startswith(('f_ext_fred_', 'f_ext_fmp_')) and train.groupby('date')[c].nunique(dropna=False).le(1).all():
            market_columns.append(c)
    if not market_columns:
        raise DirectionCandidateUnavailable('decision_time_market_features_missing')
    target = train[f'target_{horizon}']
    market_target = target.groupby(train.date).mean()
    market_x = _numeric(train, market_columns).groupby(train.date).mean()
    residual_target = target - train.date.map(market_target)
    if candidate.model == 'factor_ridge':
        market_estimator, residual_estimator = Ridge(alpha=10.), Ridge(alpha=10.)
    else:
        params = dict(max_iter=100, max_leaf_nodes=7, learning_rate=.05,
                      l2_regularization=10., random_state=seed)
        market_estimator = HistGradientBoostingRegressor(min_samples_leaf=10, **params)
        residual_estimator = HistGradientBoostingRegressor(min_samples_leaf=40, **params)
    market = _pipeline(market_estimator).fit(market_x, market_target)
    residual = _pipeline(residual_estimator).fit(_numeric(train, columns), residual_target)
    fitted = market.predict(_numeric(train, market_columns)) + residual.predict(_numeric(train, columns))
    scale = max(float(np.sqrt(np.mean((target.to_numpy() - fitted) ** 2))), 1e-8)
    details = dict(method='fixed_ten_mean_log_return_plus_shared_stock_residual',
        market_features=market_columns, market_training_dates=int(train.date.nunique()),
        residual_training_rows=len(train), probability_method='training_residual_gaussian_approximation',
        probability_scale=scale, limitation='Training residual scale may be optimistic; this is not calibrated uncertainty.')
    return _FactorDirectionModel(market, residual, market_columns, columns, scale, details), train
