"""Chronological source experts and convex fusion, fitted on the fixed ten.

The expert block precedes the weight block, which is wholly inside the outer
training period. Experts stay frozen after weight fitting. Missing sources
contribute the expert-block prior; weights are never renormalized per query.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.dummy import DummyClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .universe import TRAINING_TICKERS


FUSION_RECIPE = dict(
    version=1, groups=['price', 'us', 'night'], expert_fraction=2 / 3,
    minimum_expert_dates=20, minimum_weight_dates=10,
    expert='shared_logistic_C0.1', weight_loss='Brier_plus_L2_to_equal',
    weight_regularization=.01, minimum_prior_weight=.1,
    missing_source='expert_block_prior_without_redistribution',
    refit_experts_after_weights=False,
)


def _numeric(frame, columns):
    return frame[columns].apply(pd.to_numeric, errors='coerce').replace([np.inf, -np.inf], np.nan)


def _groups(columns):
    groups = {name: [] for name in FUSION_RECIPE['groups']}
    for column in columns:
        if column.startswith('f_ext_fmp_'):
            groups['us'].append(column)
        elif column.startswith('f_ext_taifex_'):
            groups['night'].append(column)
        elif column.startswith('f_') and not column.startswith(
                ('f_ext_', 'f_flow_', 'f_margin_', 'f_response_', 'f_openref_')):
            groups['price'].append(column)
    return groups


def _values(columns):
    # An age/update flag alone cannot establish an observed economic signal.
    return [c for c in columns if not c.endswith(('_age_days', '_window_updated'))]


class SourceFusionModel:
    """Shared experts with inspectable nominal weights and row contributions."""

    classes_ = np.array([0, 1])

    def __init__(self, experts, groups, prior):
        self.experts, self.groups, self.prior = experts, groups, prior
        self.names = ['prior', *experts]
        self.weights = np.full(len(self.names), 1 / len(self.names))

    def source_probabilities(self, frame):
        result = pd.DataFrame({'prior': np.full(len(frame), self.prior)}, index=frame.index)
        for name, expert in self.experts.items():
            columns = self.groups[name]
            values = _numeric(frame, columns)
            known = values[_values(columns)].notna().any(axis=1).to_numpy()
            probability = np.full(len(frame), self.prior)
            if known.any():
                predicted = expert.predict_proba(values.loc[known])
                probability[known] = (predicted[:, list(expert.classes_).index(1)]
                                      if 1 in expert.classes_ else 0.)
            result[name] = probability
        return result

    def contributions(self, frame):
        """Additive probability terms, not causal effects or feature importance."""
        return self.source_probabilities(frame).mul(self.weights, axis=1)

    def predict_proba(self, frame):
        probability = self.contributions(frame).sum(axis=1).to_numpy()
        return np.column_stack([1 - probability, probability])


def fit_source_fusion(train, columns, horizon, seed, *, learned):
    from .direction_models import DirectionCandidateUnavailable

    if not set(train.ticker) <= set(TRAINING_TICKERS):
        raise ValueError('Fusion may train only on the fixed ten')
    if not columns or len(set(columns)) != len(columns) or any(not c.startswith('f_') for c in columns):
        raise ValueError('Fusion requires unique feature-only columns')
    required = ['date', 'decision_ts', f'target_{horizon}', f'label_end_{horizon}',
                f'label_available_at_{horizon}']
    if any(c not in train for c in required):
        raise DirectionCandidateUnavailable('fusion_maturity_clock_missing')
    if train.duplicated(['ticker', 'date']).any():
        raise ValueError('Fusion requires unique stock dates')
    target = pd.to_numeric(train[f'target_{horizon}'], errors='coerce')
    if not np.isfinite(target).all():
        raise ValueError('Fusion requires finite mature training labels')
    dates = pd.DatetimeIndex(train.date.unique()).sort_values()
    if len(dates) < 30:
        raise DirectionCandidateUnavailable('fusion_inner_history_insufficient')
    start = dates[int(len(dates) * FUSION_RECIPE['expert_fraction'])]
    weight = train.loc[train.date.ge(start)]
    decisions = pd.to_datetime(weight.decision_ts, utc=True)
    available = pd.to_datetime(train[f'label_available_at_{horizon}'], utc=True)
    expert = train.loc[train.date.lt(start) & train[f'label_end_{horizon}'].lt(start)
                       & available.lt(decisions.min())]
    if (decisions.isna().any() or expert.date.nunique() < FUSION_RECIPE['minimum_expert_dates']
            or weight.date.nunique() < FUSION_RECIPE['minimum_weight_dates']):
        raise DirectionCandidateUnavailable('fusion_inner_history_insufficient')
    labels = expert[f'target_{horizon}'].gt(0).astype(int)
    prior = float(labels.mean())
    groups = _groups(columns)
    experts, absent = {}, []
    for name, group_columns in groups.items():
        values = _values(group_columns)
        if not values or not _numeric(expert, values).notna().any().any():
            absent.append(name)
            continue
        estimator = (DummyClassifier(strategy='prior') if labels.nunique() < 2 else
                     LogisticRegression(C=.1, max_iter=1500, random_state=seed))
        model = make_pipeline(SimpleImputer(strategy='median', keep_empty_features=True, add_indicator=True),
                              StandardScaler(), estimator)
        experts[name] = model.fit(_numeric(expert, group_columns), labels)
    model = SourceFusionModel(experts, groups, prior)
    probability = model.source_probabilities(weight).to_numpy()
    y = weight[f'target_{horizon}'].gt(0).to_numpy(dtype=float)
    # Give each observed market date equal mass without admitting external rows.
    sample_weight = 1 / weight.groupby('date').date.transform('size').to_numpy()
    sample_weight /= sample_weight.sum()
    equal = model.weights.copy()
    status = 'equal_weights'
    penalty = FUSION_RECIPE['weight_regularization']
    def loss(w):
        error = probability @ w - y
        return float(np.dot(sample_weight, error ** 2) + penalty * np.sum((w - equal) ** 2))
    def gradient(w):
        return 2 * probability.T @ (sample_weight * (probability @ w - y)) + 2 * penalty * (w - equal)
    if learned and len(equal) > 1:
        result = minimize(loss, equal, jac=gradient, method='SLSQP',
                          bounds=[(FUSION_RECIPE['minimum_prior_weight'], 1.)]
                                 + [(0., 1.)] * (len(equal) - 1),
                          constraints=[dict(type='eq', fun=lambda w: w.sum() - 1,
                                            jac=lambda w: np.ones(len(w)))],
                          options=dict(ftol=1e-12, maxiter=200))
        if (result.success and np.isfinite(result.x).all() and result.x.min() >= -1e-10
                and abs(result.x.sum() - 1) < 1e-8 and loss(result.x) <= loss(equal) + 1e-10):
            model.weights = np.clip(result.x, 0., 1.)
            model.weights /= model.weights.sum()
            status = 'optimized'
        else:
            status = 'optimization_failed_use_equal'
    used = pd.concat([expert, weight]).sort_index()
    model.training_details = dict(
        method='chronological_source_probability_fusion', recipe=dict(FUSION_RECIPE),
        learned=learned, status=status, groups=groups, absent_groups=absent,
        ignored_features=[c for c in columns if not any(c in g for g in groups.values())],
        weights=dict(zip(model.names, map(float, model.weights), strict=True)),
        expert_training_rows=len(expert), expert_training_dates=int(expert.date.nunique()),
        expert_label_end_max=str(expert[f'label_end_{horizon}'].max()),
        expert_label_available_max=str(pd.to_datetime(expert[f'label_available_at_{horizon}'], utc=True).max()),
        weight_start=str(start), weight_decision_min=str(decisions.min()),
        weight_training_rows=len(weight), weight_training_dates=int(weight.date.nunique()),
        purged_rows=len(train) - len(used), training_probability=prior,
        weight_block_brier=float(np.dot(sample_weight, (probability @ model.weights - y) ** 2)),
        equal_weight_block_brier=float(np.dot(sample_weight, (probability @ equal - y) ** 2)),
        limitation='Weights describe fitted probability fusion, not causal impacts. Static within a fold; '
                   'no news encoder, volatility head, or verified original receipt evidence is supplied.',
    )
    return model, used
