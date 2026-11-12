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


def _bounded_beta(frame):
    return _numeric(frame, ['f_beta_60']).iloc[:, 0].fillna(1.).clip(-3., 3.)


def _decision_volatility(frame, default, floor):
    values = _numeric(frame, ['f_volatility_20']).iloc[:, 0]
    return values.where(values.gt(0)).fillna(default).clip(lower=floor)


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
    beta_scaled: bool = False
    volatility_default: float | None = None
    volatility_floor: float | None = None

    @property
    def classes_(self):
        return np.array([0, 1])

    def predict_proba(self, frame):
        market = self.market.predict(_numeric(frame, self.market_columns))
        if self.beta_scaled:
            market = market * _bounded_beta(frame).to_numpy()
        center = market + self.residual.predict(_numeric(frame, self.columns))
        denominator = self.scale
        if self.volatility_default is not None:
            denominator = self.scale * _decision_volatility(frame, self.volatility_default, self.volatility_floor).to_numpy()
        p = ndtr(center / denominator)
        return np.column_stack([1 - p, p])


@dataclass
class _OpeningDirectionModel:
    remaining: object
    columns: list[str]
    scale: float
    training_details: dict
    volatility_default: float | None = None
    volatility_floor: float | None = None

    @property
    def classes_(self):
        return np.array([0, 1])

    def predict_proba(self, frame):
        gap = _numeric(frame, ['f_openref_log_gap']).iloc[:, 0].fillna(0.).to_numpy()
        center = gap + self.remaining.predict(_numeric(frame, self.columns))
        denominator = self.scale
        if self.volatility_default is not None:
            denominator = self.scale * _decision_volatility(frame, self.volatility_default, self.volatility_floor).to_numpy()
        p = ndtr(center / denominator)
        return np.column_stack([1 - p, p])


def _fit_opening_residual(candidate, train, columns, horizon, seed):
    if 'f_openref_log_gap' not in columns or not _numeric(train, ['f_openref_log_gap']).notna().any().any():
        raise DirectionCandidateUnavailable('opening_reference_training_missing')
    gap = _numeric(train, ['f_openref_log_gap']).iloc[:, 0]
    target = train[f'target_{horizon}'] - gap.fillna(0.)
    if candidate.model.endswith('_ridge'):
        estimator = Ridge(alpha=10.)
    else:
        estimator = HistGradientBoostingRegressor(max_iter=100, max_leaf_nodes=7,
            min_samples_leaf=40, learning_rate=.05, l2_regularization=10., random_state=seed)
    volatility_default, volatility_floor = None, None
    if '_vol_' in candidate.model:
        if 'f_volatility_20' not in columns:
            raise DirectionCandidateUnavailable('decision_time_volatility_missing')
        values = _numeric(train, ['f_volatility_20']).iloc[:, 0]
        known = values.where(values.gt(0)).dropna()
        if known.empty:
            raise DirectionCandidateUnavailable('decision_time_volatility_missing')
        volatility_default = float(known.median())
        volatility_floor = max(volatility_default * .1, 1e-4)
    model = _pipeline(estimator).fit(_numeric(train, columns), target)
    error = target.to_numpy() - model.predict(_numeric(train, columns))
    if volatility_default is not None:
        error = error / _decision_volatility(train, volatility_default, volatility_floor).to_numpy()
    scale = max(float(np.sqrt(np.mean(error ** 2))), 1e-8)
    details = dict(method='assumed_opening_gap_plus_shared_remaining_log_return',
        gap_feature='f_openref_log_gap', gap_missing_fallback=0.,
        gap_missing_training_rows=int(gap.isna().sum()), residual_training_rows=len(train),
        probability_method='training_residual_volatility_scaled_gaussian_approximation' if volatility_default is not None else 'training_residual_gaussian_approximation',
        probability_scale=scale, volatility_default=volatility_default, volatility_floor=volatility_floor,
        limitation='Counterfactual raw opening reference, not archived preopen information. Raw corporate-action effects remain. Training residual scale is not calibrated uncertainty.')
    return _OpeningDirectionModel(model, columns, scale, details, volatility_default, volatility_floor), train


def fit_direction_model(candidate, train, columns, horizon, seed):
    """Fit only the supplied mature training rows and return rows actually used.

    Factor models learn one market target per complete fixed-ten date and one
    pooled stock residual. Beta variants use a cross-sectional projection onto
    decision-time exposures, then scale the predicted factor by each stock's
    known exposure. Optional volatility scores use training-only fallback and
    residual scale. Gaussian scores are not calibrated uncertainty guarantees.
    """
    if not set(train.ticker) <= set(TRAINING_TICKERS):
        raise ValueError('Direction models may train only on the fixed ten')
    if candidate.window_dates is not None:
        dates = pd.DatetimeIndex(train.date.unique()).sort_values()
        train = train.loc[train.date >= dates[-min(candidate.window_dates, len(dates))]]
    if candidate.model.startswith('opening_residual_'):
        return _fit_opening_residual(candidate, train, columns, horizon, seed)
    if candidate.model.startswith('similarity_'):
        from .direction_similarity import RADII, SimilarityDirectionModel
        labels = train[f'target_{horizon}'].gt(0).to_numpy(dtype=int)
        return SimilarityDirectionModel(train, columns, labels, RADII[candidate.model]), train
    if not candidate.model.startswith('factor_'):
        labels = train[f'target_{horizon}'].gt(0).to_numpy(dtype=int)
        model = _classifier(candidate, seed, labels).fit(_numeric(train, columns), labels)
        return model, train
    complete = train.groupby('date').ticker.nunique().eq(len(TRAINING_TICKERS))
    train = train.loc[train.date.isin(complete[complete].index)].copy()
    if train.date.nunique() < 10:
        raise DirectionCandidateUnavailable('incomplete_fixed_ten_market_history')
    beta_scaled = candidate.model.startswith('factor_beta_')
    volatility_scaled = candidate.model.startswith('factor_beta_vol_')
    exposure_ineligible_dates = 0
    if beta_scaled:
        if 'f_beta_60' not in columns or not _numeric(train, ['f_beta_60']).notna().any().any():
            raise DirectionCandidateUnavailable('decision_time_beta_missing')
        exposure_energy = (_bounded_beta(train) ** 2).groupby(train.date).sum()
        eligible = exposure_energy.gt(1e-8)
        exposure_ineligible_dates = int((~eligible).sum())
        train = train.loc[train.date.isin(eligible[eligible].index)].copy()
        if train.date.nunique() < 10:
            raise DirectionCandidateUnavailable('market_exposure_not_identifiable')
    market_columns = [c for c in columns if c.startswith('f_market_')]
    # Global context must actually be broadcast equally across the fixed ten.
    # Company-specific or partially populated external features remain solely
    # in the stock residual fit.
    for c in columns:
        if c.startswith(('f_ext_fred_', 'f_ext_fmp_', 'f_ext_taifex_', 'f_ext_us_sector_')) and train.groupby('date')[c].nunique(dropna=False).le(1).all():
            market_columns.append(c)
    if not market_columns:
        raise DirectionCandidateUnavailable('decision_time_market_features_missing')
    target = train[f'target_{horizon}']
    if beta_scaled:
        beta = _bounded_beta(train)
        market_target = (beta * target).groupby(train.date).sum() / (beta ** 2).groupby(train.date).sum()
        residual_target = target - beta * train.date.map(market_target)
    else:
        market_target = target.groupby(train.date).mean()
        residual_target = target - train.date.map(market_target)
    market_x = _numeric(train, market_columns).groupby(train.date).mean()
    if candidate.model.endswith('_ridge'):
        market_estimator, residual_estimator = Ridge(alpha=10.), Ridge(alpha=10.)
    else:
        params = dict(max_iter=100, max_leaf_nodes=7, learning_rate=.05,
                      l2_regularization=10., random_state=seed)
        market_estimator = HistGradientBoostingRegressor(min_samples_leaf=10, **params)
        residual_estimator = HistGradientBoostingRegressor(min_samples_leaf=40, **params)
    market = _pipeline(market_estimator).fit(market_x, market_target)
    residual = _pipeline(residual_estimator).fit(_numeric(train, columns), residual_target)
    fitted_market = market.predict(_numeric(train, market_columns))
    if beta_scaled:
        fitted_market = fitted_market * beta.to_numpy()
    fitted = fitted_market + residual.predict(_numeric(train, columns))
    volatility_default, volatility_floor = None, None
    error = target.to_numpy() - fitted
    if volatility_scaled:
        if 'f_volatility_20' not in columns:
            raise DirectionCandidateUnavailable('decision_time_volatility_missing')
        values = _numeric(train, ['f_volatility_20']).iloc[:, 0]
        known = values.where(values.gt(0)).dropna()
        if known.empty:
            raise DirectionCandidateUnavailable('decision_time_volatility_missing')
        volatility_default = float(known.median())
        volatility_floor = max(volatility_default * .1, 1e-4)
        error = error / _decision_volatility(train, volatility_default, volatility_floor).to_numpy()
    scale = max(float(np.sqrt(np.mean(error ** 2))), 1e-8)
    details = dict(method='fixed_ten_mean_log_return_plus_shared_stock_residual',
        market_features=market_columns, market_training_dates=int(train.date.nunique()),
        residual_training_rows=len(train), probability_method='training_residual_gaussian_approximation',
        probability_scale=scale, limitation='Training residual scale may be optimistic; this is not calibrated uncertainty.')
    if beta_scaled:
        raw_beta = _numeric(train, ['f_beta_60']).iloc[:, 0]
        details.update(method='fixed_ten_beta_projection_plus_shared_stock_residual',
            beta_clip=[-3., 3.], beta_missing_fallback=1.,
            beta_missing_training_rows=int(raw_beta.isna().sum()),
            beta_clipped_training_rows=int(raw_beta.abs().gt(3.).sum()),
            exposure_ineligible_dates=exposure_ineligible_dates, minimum_exposure_energy=1e-8)
    if volatility_scaled:
        details.update(probability_method='training_residual_volatility_scaled_gaussian_approximation',
            volatility_default=volatility_default, volatility_floor=volatility_floor,
            volatility_missing_training_rows=int((~values.gt(0)).sum()))
    return _FactorDirectionModel(market, residual, market_columns, columns, scale, details,
                                 beta_scaled, volatility_default, volatility_floor), train
