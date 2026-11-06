"""Chronological research, calibrated distributions, and executable paper accounting.

All targets are log adjusted-close returns. Outputs and portfolio returns are
simple returns. Calibration and test samples are separated by label maturity,
not merely by row number. The simulator uses next-open to same-day-close lots,
and therefore never credits a portfolio with the prior close's entry price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits
from .universe import TRAINING_TICKERS,asset_class,tickers


@dataclass(frozen=True)
class ResearchConfig:
    horizons: tuple[int, ...] = (1, 5, 20)
    min_train_dates: int = 252
    calibration_dates: int = 63
    test_dates: int = 63
    max_folds: int = 5
    embargo_dates: int = 1
    model: str = "hist_gradient_boosting"
    commission: float = 0.001425
    sell_tax: float = 0.003
    etf_sell_tax: float = 0.001
    slippage: float = 0.001
    max_positions: int = 5
    max_weight: float = 0.2
    min_probability: float = 0.55
    min_expected_return: float = 0.0
    random_state: int = 42
    evaluation_tickers: tuple[str,...] = ()
    feature_profile: str = 'legacy'
    objective: str = 'close_return'
    training_window_dates: int | None = None
    calibration_policy: str = 'sigmoid'

    def __post_init__(self):
        if self.evaluation_tickers:
            if set(tickers(self.evaluation_tickers))&set(TRAINING_TICKERS):
                raise ValueError('Evaluation tickers must be outside the fixed training ten')
        if self.feature_profile not in {'legacy','portable_price','portable_market','portable_all'}:
            raise ValueError('Unknown feature profile')
        if self.objective not in {'close_return','open_return'}:raise ValueError('Unknown objective')
        if self.calibration_policy not in {'sigmoid','chronological_shrinkage'}:raise ValueError('Unknown calibration policy')
        if self.training_window_dates is not None and self.training_window_dates<self.min_train_dates:
            raise ValueError('Training window is shorter than minimum history')
        if not self.horizons or any(h not in (1, 5, 20) for h in self.horizons):
            raise ValueError("horizons must be selected from 1, 5, 20")
        if min(self.min_train_dates, self.calibration_dates, self.test_dates, self.max_folds) < 1:
            raise ValueError("training, calibration, test dates and max_folds must be positive")
        if self.min_train_dates < 10 or self.calibration_dates < 5 or self.embargo_dates < 0:
            raise ValueError("need at least 10 training dates, 5 calibration dates and nonnegative embargo")
        if self.model != "hist_gradient_boosting":
            raise ValueError("model currently supports hist_gradient_boosting")
        if not 0 < self.max_weight <= 1 or self.max_positions < 1:
            raise ValueError("invalid portfolio position limits")
        if not 0 <= self.min_probability <= 1:
            raise ValueError("min_probability must be in [0, 1]")
        if any(not 0 <= v < 0.2 for v in (self.commission, self.sell_tax, self.etf_sell_tax, self.slippage)):
            raise ValueError("transaction cost rates must be in [0, 0.2)")


@dataclass
class ResearchResult:
    predictions: pd.DataFrame
    metrics: dict[str, Any]
    folds: pd.DataFrame
    equity: pd.DataFrame
    models: dict[int, "TrainedModel"] = field(default_factory=dict)
    transfer_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)


def _numeric(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return frame[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _validate(frame: pd.DataFrame, feature_cols: list[str], config: ResearchConfig,training=True) -> pd.DataFrame:
    if not feature_cols or any(not c.startswith("f_") for c in feature_cols):
        raise ValueError("feature columns must be nonempty and must not contain future targets or execution outcomes")
    required = {"ticker", "date", "decision_ts", *feature_cols}
    for h in config.horizons:
        required |= {f"target_{h}", f"label_end_{h}"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"research frame missing columns: {sorted(missing)}")
    universe=TRAINING_TICKERS if training else config.evaluation_tickers
    out = frame.loc[frame.ticker.astype(str).isin(universe)].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["decision_ts"] = pd.to_datetime(out["decision_ts"], utc=True)
    out = out.loc[out.decision_ts.notna()].copy()
    if out.duplicated(["ticker", "date"]).any():
        raise ValueError("research frame contains duplicate ticker/date observations")
    for h in config.horizons:
        if config.objective=='open_return':
            if f'execution_target_{h}' not in out:raise ValueError('Executable-return objective requires execution targets')
            out[f'target_{h}']=out[f'execution_target_{h}']
        out[f"label_end_{h}"] = pd.to_datetime(out[f"label_end_{h}"])
        vals = pd.to_numeric(out[f"target_{h}"], errors="coerce")
        out[f"target_{h}"] = vals.where(np.isfinite(vals))
    return out.sort_values(["date", "ticker"]).reset_index(drop=True)


def predict_evaluation(model,frame,horizon,evaluation_cutoff=None):
    prediction=model.predict(frame)
    actual=np.expm1(pd.to_numeric(frame[f'target_{horizon}'],errors='coerce'))
    if evaluation_cutoff is not None and f'label_available_at_{horizon}' in frame:
        known=pd.to_datetime(frame[f'label_available_at_{horizon}'],utc=True).le(evaluation_cutoff)
        actual=actual.where(known)
    prediction['actual_return']=actual.where(np.isfinite(actual))
    prediction['label_end_date']=frame[f'label_end_{horizon}']
    for column in ['close','adj_close','next_open','next_open_date','asset_class',f'label_available_at_{horizon}',
                   f'trade_return_{horizon}',f'benchmark_trade_return_{horizon}',f'benchmark_target_{horizon}',
                   f'trade_valid_{horizon}','benchmark_adj_close','benchmark_adjusted_next_open',f'benchmark_end_adj_close_{horizon}']:
        if column in frame:prediction[column]=frame[column]
    return prediction


def transfer_metrics(predictions):
    if predictions.empty:return {}
    return {str(ticker):dict(tradable=asset_class(ticker)!='index',scope='same_model_unseen_asset',
        return_basis='price_index' if asset_class(ticker)=='index' else 'corporate_action_adjusted',
        by_horizon={str(h):_horizon_metrics(g) for h,g in group.groupby('horizon')})
        for ticker,group in predictions.groupby('ticker')}


def temporal_splits(frame: pd.DataFrame, horizon: int, config: ResearchConfig, *, calendar_dates=None) -> list[dict]:
    """Return row-index splits with date grouping, maturity purging and embargo.

    The beginning of history is retained for expanding-window fits. Up to
    max_folds recent non-overlapping test blocks are reported. At each seam the
    labels must finish strictly before the next segment, including an embargo
    measured in observed market dates. An explicit observed market calendar
    keeps dates where every stock row is absent, and defines the latest mature
    anchor by horizon length rather than a future quote's presence. Callers
    retain control of whether benchmark outcomes are required in the frame.
    """
    dates = pd.DatetimeIndex(pd.to_datetime(frame.date).dropna().unique()).sort_values()
    if calendar_dates is not None:
        dates = pd.DatetimeIndex(pd.to_datetime(calendar_dates)).normalize().unique().sort_values()
        if not len(dates) or dates.hasnans or dates.tz is not None or not set(frame.date) <= set(dates):
            raise ValueError('Reference market calendar must contain every frame date as naive daily dates')
    label = f"label_end_{horizon}"
    target = f"target_{horizon}"
    first_test = config.min_train_dates + config.calibration_dates + 2 * (horizon + config.embargo_dates)
    globally_mature = frame[label].notna()
    if f"benchmark_target_{horizon}" in frame:
        globally_mature &= frame[f"benchmark_target_{horizon}"].notna()
    last_mature = frame.loc[globally_mature, "date"].max()
    if calendar_dates is not None:
        last_mature = dates[-horizon - 1] if len(dates) > horizon else pd.NaT
    starts = [start for start in range(first_test, len(dates), config.test_dates)
              if pd.notna(last_mature) and dates[start] <= last_mature][-config.max_folds:]
    folds = []
    finite = pd.to_numeric(frame[target], errors="coerce").notna() & frame[label].notna()
    for start in starts:
        test_dates = dates[start:start + config.test_dates]
        if calendar_dates is not None:
            test_dates = test_dates[test_dates <= last_mature]
        if not len(test_dates):
            continue
        cal_stop = start - config.embargo_dates
        cal_start = cal_stop - config.calibration_dates - horizon
        train_stop = cal_start - config.embargo_dates
        if train_stop <= 0:
            continue
        train_mask = ((frame.date < dates[train_stop]) & (frame[label] < dates[train_stop]) & finite)
        calibration_mask = ((frame.date >= dates[cal_start]) & (frame.date < dates[cal_stop])
                            & (frame[label] < dates[cal_stop]) & finite)
        available_column = f"label_available_at_{horizon}"
        if available_column in frame:
            observed = pd.to_datetime(frame[available_column], utc=True)
            train_cutoff = dates[train_stop].tz_localize("Asia/Taipei").tz_convert("UTC")
            calibration_cutoff = dates[cal_stop].tz_localize("Asia/Taipei").tz_convert("UTC")
            train_mask &= observed.lt(train_cutoff)
            calibration_mask &= observed.lt(calibration_cutoff)
        # Test membership is fixed by decision-time eligibility and the global
        # benchmark's matured horizon, never by a stock's future price presence.
        test_mask = frame.date.isin(test_dates) & frame[label].notna()
        if f"benchmark_target_{horizon}" in frame:
            test_mask &= frame[f"benchmark_target_{horizon}"].notna()
        train_idx = frame.index[train_mask].tolist()
        cal_idx = frame.index[calibration_mask].tolist()
        test_idx = frame.index[test_mask].tolist()
        if not train_idx or not cal_idx or (not test_idx and calendar_dates is None):
            continue
        if frame.loc[train_idx, "date"].nunique() < config.min_train_dates:
            continue
        if frame.loc[cal_idx, "date"].nunique() < config.calibration_dates:
            continue
        folds.append({"train": train_idx, "calibration": cal_idx, "test": test_idx,
                      "test_calendar": test_dates.tolist()})
    if not folds:
        raise ValueError(f"insufficient history dates for horizon {horizon}; need at least {first_test + 1} sessions with mature labels")
    return folds


def _linear_regressor() -> Pipeline:
    return Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                     ("scaler", StandardScaler()), ("model", Ridge(alpha=10.0))])


def _classifier(y: np.ndarray, random_state: int) -> Pipeline:
    estimator = (LogisticRegression(C=0.1, max_iter=1000, random_state=random_state)
                 if len(np.unique(y)) > 1 else DummyClassifier(strategy="prior"))
    return Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                     ("scaler", StandardScaler()), ("model", estimator)])


def _positive_probability(model, x) -> np.ndarray:
    p = model.predict_proba(x)
    classes = np.asarray(model.classes_)
    if 1 not in classes:
        return np.zeros(len(x))
    return p[:, int(np.flatnonzero(classes == 1)[0])]


def _logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p), 1e-5, 1 - 1e-5)
    return np.log(p / (1 - p)).reshape(-1, 1)


@dataclass
class TrainedModel:
    horizon: int
    feature_columns: list[str]
    regressor: Any
    classifier: Any
    baseline_regressor: Pipeline
    baseline_classifier: Pipeline
    calibrator: Any
    constant_probability: float | None
    residual_radius: float
    return_bias: float
    model_version: str
    trained_through: str
    calibration_ends: str
    training_cutoff: str = ""
    training_probability: float = .5
    calibration_prior_weight: float = 0.
    calibration_selection: dict = field(default_factory=dict)
    arithmetic_regressor: Any = None
    training_start: str = ''
    training_dates: int = 0
    training_rows: int = 0

    @property
    def train_end(self):
        return self.trained_through

    @property
    def calibration_end(self):
        return self.calibration_ends

    @threadpool_limits.wrap(limits=1)
    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Predict using frozen training-only transforms and temporal calibration."""
        x = _numeric(frame, self.feature_columns)
        raw_log = 0.5 * (self.regressor.predict(x) + self.baseline_regressor.predict(x))
        p0 = 0.5 * (_positive_probability(self.classifier, x) + _positive_probability(self.baseline_classifier, x))
        if self.constant_probability is not None:
            probability = np.full(len(frame), self.constant_probability)
        else:
            probability = self.calibrator.predict_proba(_logit(p0))[:, 1]
        probability=(1-self.calibration_prior_weight)*probability+self.calibration_prior_weight*self.training_probability
        center = raw_log + self.return_bias
        # Clip logs to keep JSON finite even for malformed or extreme research inputs.
        center = np.clip(center, -10, 10)
        out = frame[[c for c in ["ticker", "date", "decision_ts"] if c in frame]].copy()
        out["horizon"] = self.horizon
        out["probability_up"] = np.clip(probability, 0, 1)
        out["expected_return"] = np.expm1(center)
        out['arithmetic_expected_return']=np.clip(self.arithmetic_regressor.predict(x),-.999,np.expm1(10)) if self.arithmetic_regressor is not None else np.nan
        out['training_probability']=self.training_probability
        out["q10"] = np.expm1(np.clip(center - self.residual_radius, -10, 10))
        out["q50"] = np.expm1(center)
        out["q90"] = np.expm1(np.clip(center + self.residual_radius, -10, 10))
        out["baseline_probability"] = _positive_probability(self.baseline_classifier, x)
        out["baseline_return"] = np.expm1(np.clip(self.baseline_regressor.predict(x), -10, 10))
        out["model_version"] = self.model_version
        out["explanation"] = [json.dumps(row, ensure_ascii=False) for row in self.explain(frame, top_n=3)]
        return out

    def explain(self, frame: pd.DataFrame, top_n: int = 5) -> list[list[dict]]:
        """Standardized linear-baseline contributions, not ensemble SHAP or causality."""
        x = _numeric(frame, self.feature_columns)
        transformed = self.baseline_regressor[:-1].transform(x)
        contributions = transformed * self.baseline_regressor[-1].coef_
        return [[{"feature": self.feature_columns[i], "linear_contribution": round(float(row[i]), 8),
                  "method": "standardized_linear_baseline_proxy"}
                 for i in np.argsort(np.abs(row))[-max(1, top_n):][::-1]] for row in contributions]


def _calibration_weight(p,labels,calibration,horizon,prior,random_state):
    dates=pd.DatetimeIndex(calibration.date.unique()).sort_values()
    validation_start=dates[min(len(dates)-1,max(1,len(dates)*2//3))]
    fit=(calibration.date<validation_start)&(calibration[f'label_end_{horizon}']<validation_start)
    if f'label_available_at_{horizon}' in calibration:
        fit &= pd.to_datetime(calibration[f'label_available_at_{horizon}'],utc=True)<validation_start.tz_localize('Asia/Taipei').tz_convert('UTC')
    validation=calibration.date>=validation_start
    metadata=dict(validation_start=validation_start.isoformat(),
        fit_label_end_max=str(calibration.loc[fit,f'label_end_{horizon}'].max()),
        method='chronological_calibration_only',candidate_weights=[0.,.25,.5,.75,1.])
    if fit.sum()<10 or len(np.unique(labels[fit.to_numpy()]))<2 or not validation.any():
        return 1.,{**metadata,'status':'insufficient_inner_calibration_use_training_prior'}
    inner=LogisticRegression(C=1.,max_iter=1000,random_state=random_state).fit(_logit(p[fit.to_numpy()]),labels[fit.to_numpy()])
    raw=inner.predict_proba(_logit(p[validation.to_numpy()]))[:,1]
    scores={w:float(np.mean(((1-w)*raw+w*prior-labels[validation.to_numpy()])**2)) for w in (0.,.25,.5,.75,1.)}
    weight=min(scores,key=lambda w:(scores[w],-w))
    return weight,{**metadata,'status':'selected','validation_brier':scores}


@threadpool_limits.wrap(limits=1)
def _fit(train: pd.DataFrame, calibration: pd.DataFrame, feature_cols: list[str], horizon: int, config: ResearchConfig) -> TrainedModel:
    if config.training_window_dates is not None:
        dates=pd.DatetimeIndex(train.date.unique()).sort_values()
        train=train[train.date>=dates[-min(config.training_window_dates,len(dates))]]
    x = _numeric(train, feature_cols)
    y = train[f"target_{horizon}"].to_numpy(dtype=float)
    yc = (y > 0).astype(int)
    baseline_regressor = _linear_regressor().fit(x, y)
    baseline_classifier = _classifier(yc, config.random_state).fit(x, yc)
    regressor = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("model", HistGradientBoostingRegressor(max_iter=45, max_leaf_nodes=15, min_samples_leaf=20,
            learning_rate=0.06, l2_regularization=5.0, random_state=config.random_state))]).fit(x, y)
    arithmetic=Pipeline([('imputer',SimpleImputer(strategy='median',keep_empty_features=True)),
        ('model',HistGradientBoostingRegressor(max_iter=45,max_leaf_nodes=15,min_samples_leaf=20,
            learning_rate=.06,l2_regularization=5.,random_state=config.random_state))]).fit(x,np.expm1(np.clip(y,-10,10)))
    if len(np.unique(yc)) < 2:
        classifier = _classifier(yc, config.random_state).fit(x, yc)
    else:
        classifier = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("model", HistGradientBoostingClassifier(max_iter=45, max_leaf_nodes=15, min_samples_leaf=20,
                learning_rate=0.06, l2_regularization=5.0, random_state=config.random_state))]).fit(x, yc)
    xc = _numeric(calibration, feature_cols)
    actual = calibration[f"target_{horizon}"].to_numpy(dtype=float)
    raw_log = 0.5 * (regressor.predict(xc) + baseline_regressor.predict(xc))
    # The point predictor is frozen before calibration. Refitting an intercept
    # on these same residuals would invalidate a split-conformal construction.
    bias = 0.0
    scores = np.abs(actual - raw_log - bias)
    quantile = min(1.0, np.ceil((len(scores) + 1) * 0.8) / len(scores))
    radius = float(np.quantile(scores, quantile, method="higher"))
    p = 0.5 * (_positive_probability(classifier, xc) + _positive_probability(baseline_classifier, xc))
    labels = (actual > 0).astype(int)
    calibrator = None
    constant = None
    if len(np.unique(labels)) < 2:
        constant = float((labels.sum() + 1) / (len(labels) + 2))
    else:
        calibrator = LogisticRegression(C=1.0, max_iter=1000, random_state=config.random_state).fit(_logit(p), labels)
    prior=float((yc.sum()+1)/(len(yc)+2))
    prior_weight,selection=_calibration_weight(p,labels,calibration,horizon,prior,config.random_state) if config.calibration_policy=='chronological_shrinkage' else (0.,{'method':'sigmoid'})
    digest = hashlib.sha256()
    digest.update(json.dumps({"config": asdict(config), "features": feature_cols, "horizon": horizon}, sort_keys=True).encode())
    for dataset in (train, calibration):
        digest.update(pd.util.hash_pandas_object(dataset[["ticker", "date", *feature_cols, f"target_{horizon}"]], index=False).to_numpy().tobytes())
    version = f"twq-v1-h{horizon}-{digest.hexdigest()[:16]}"
    model=TrainedModel(horizon, feature_cols, regressor, classifier, baseline_regressor, baseline_classifier,
                        calibrator, constant, radius, bias, version, str(train.date.max().date()),
                        str(calibration[f"label_end_{horizon}"].max().date()))
    model.training_probability=prior;model.calibration_prior_weight=prior_weight
    model.calibration_selection=selection;model.arithmetic_regressor=arithmetic
    model.training_start=str(train.date.min().date())
    model.training_dates=int(train.date.nunique());model.training_rows=len(train)
    return model


def _horizon_metrics(p: pd.DataFrame) -> dict:
    p = p.loc[p.actual_return.notna() & np.isfinite(p.actual_return)].copy()
    if p.empty:
        return {"observations": 0, "test_dates": 0, "folds": 0, "accuracy": None,
                "baseline_accuracy": None, "brier": None, "baseline_brier": None, "brier_skill": 0.0,
                "log_loss": None, "mae": None, "baseline_mae": None, "rmse": None,
                "coverage80": None, "interval_width": None, "rank_ic": None}
    actual = p.actual_return.to_numpy(dtype=float)
    labels = (actual > 0).astype(int)
    probability = p.probability_up.to_numpy(dtype=float)
    baseline = p.baseline_probability.to_numpy(dtype=float)
    brier = float(brier_score_loss(labels, probability))
    baseline_brier = float(brier_score_loss(labels, baseline))
    ics = []
    for _, group in p.groupby("date"):
        if len(group) >= 3 and group.actual_return.nunique() > 1 and group.expected_return.nunique() > 1:
            ics.append(float(group.actual_return.rank().corr(group.expected_return.rank())))
    positive=probability>=.5;up=labels==1;down=~up
    up_recall=float(positive[up].mean()) if up.any() else None
    down_recall=float((~positive[down]).mean()) if down.any() else None
    frequency=pd.to_numeric(p.get('training_probability',pd.Series(.5,index=p.index)),errors='raise').to_numpy()
    frequency_brier=float(np.mean((frequency-labels)**2))
    reliability=[]
    for low in (0.,.2,.4,.6,.8):
        mask=(probability>=low)&(probability<round(low+.2,2) if low<.8 else probability<=1.)
        reliability.append(dict(low=low,count=int(mask.sum()),predicted=float(probability[mask].mean()) if mask.any() else None,
                                observed=float(labels[mask].mean()) if mask.any() else None))
    return {
        'predicted_up_fraction':float(positive.mean()),'observed_up_fraction':float(up.mean()),
        'always_up_accuracy':float(up.mean()),'fixed_p50_brier':.25,'frequency_brier':frequency_brier,
        'brier_skill_vs_p50':float(1-brier/.25),'brier_skill_vs_frequency':float(1-brier/frequency_brier) if frequency_brier>0 else None,
        'balanced_accuracy':float((up_recall+down_recall)/2) if up_recall is not None and down_recall is not None else None,
        'recall_up':up_recall,'recall_down':down_recall,'reliability':reliability,
        'confusion':dict(true_up=int((positive&up).sum()),false_up=int((positive&down).sum()),true_down=int((~positive&down).sum()),false_down=int((~positive&up).sum())),
        "observations": len(p), "test_dates": p.date.nunique(), "folds": p.fold.nunique(),
        "accuracy": float(np.mean((probability >= .5) == labels)),
        "baseline_accuracy": float(np.mean((baseline >= .5) == labels)),
        "brier": brier, "baseline_brier": baseline_brier,
        "brier_skill": float(1 - brier / baseline_brier) if baseline_brier > 0 else 0.0,
        "log_loss": float(log_loss(labels, np.clip(probability, 1e-8, 1 - 1e-8), labels=[0, 1])),
        "mae": float(mean_absolute_error(actual, p.expected_return)),
        "baseline_mae": float(mean_absolute_error(actual, p.baseline_return)),
        "rmse": float(np.sqrt(mean_squared_error(actual, p.expected_return))),
        "coverage80": float(np.mean((p.actual_return >= p.q10) & (p.actual_return <= p.q90))),
        "interval_width": float(np.mean(p.q90 - p.q10)),
        "rank_ic": float(np.mean(ics)) if ics else None,
        "interval_method": "80% pooled temporal residual interval; dependent market data has no exchangeability guarantee",
    }


def _performance(returns: np.ndarray, equity: np.ndarray) -> dict:
    if not len(returns):
        return {"total_return": 0.0, "annualized_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    running = np.maximum.accumulate(np.r_[1., equity])
    dd = np.r_[1., equity] / running - 1
    sd = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    return {
        "total_return": float(equity[-1] - 1),
        "annualized_return": float(equity[-1] ** (252 / len(returns)) - 1) if equity[-1] > 0 else -1.0,
        "sharpe": float(np.mean(returns) / sd * np.sqrt(252)) if sd > 1e-12 else 0.0,
        "max_drawdown": float(dd.min()),
    }


def simulate_portfolio(predictions: pd.DataFrame, config: ResearchConfig) -> tuple[pd.DataFrame, dict]:
    """Long-only h=1 lots bought next open, sold that close, flat overnight.

    Fees apply to buy and sell notionals. Allocation includes the entry fee so
    the cash balance cannot become negative. 5/20-day forecasts are evaluated as
    prediction tasks; overlapping labels never become a compounded NAV.
    """
    p = predictions.loc[predictions.horizon == 1].copy()
    required = {"trade_return_1", "benchmark_trade_return_1", "next_open", "next_open_date"}
    if p.empty or not required.issubset(p):
        return pd.DataFrame(columns=["date", "strategy_net", "benchmark_net", "equal_weight_net"]), {
            "status": "unavailable", "reason": "horizon 1 executable prices and benchmark required", "orders": 0}
    p = p.sort_values(["date", "ticker"])
    buy = config.commission + config.slippage
    sell = config.commission + config.slippage + config.sell_tax
    etf_sell = config.commission + config.slippage + config.etf_sell_tax
    # A conservative cost screen uses close-to-close return estimates, although
    # the overnight gap cannot be captured by this open-to-close strategy.
    roundtrip = 1 - (1 - sell) / (1 + buy)
    rows = []
    orders = 0
    unexecutable = 0
    benchmark_unexecutable = 0
    for anchor, group in p.groupby("date", sort=True):
        benchmark_vals = group.benchmark_trade_return_1.dropna()
        if group.next_open_date.dropna().empty:
            continue
        valid = (group.trade_return_1.notna() & np.isfinite(group.trade_return_1)
                 & group.next_open.gt(0) & group.next_open_date.notna())
        if "trade_valid_1" in group:
            valid &= group.trade_valid_1.fillna(False).astype(bool)
        score='arithmetic_expected_return' if 'arithmetic_expected_return' in group else 'expected_return'
        candidates = group.loc[group.probability_up.ge(config.min_probability)
                               & group[score].gt(roundtrip + config.min_expected_return)]
        selected = candidates.sort_values([score, "ticker"], ascending=[False, True]).head(config.max_positions)
        # Select on decision-time information only. If a selected order cannot
        # execute, its allocation stays cash; future fillability cannot promote
        # another stock into the selected portfolio.
        eligible = selected.loc[valid.reindex(selected.index)]
        weight = min(config.max_weight, 1 / config.max_positions)
        allocation = weight * len(eligible)
        lot_returns = ((1 + eligible.trade_return_1) * (1 - sell) / (1 + buy) - 1)
        strategy_return = float(weight * lot_returns.sum())
        strategy_gross=float(weight*eligible.trade_return_1.sum())
        benchmark_gross=float(benchmark_vals.iloc[0]) if len(benchmark_vals) else np.nan
        # The overnight-flat ETF strategy stays cash when its opening trade is
        # unavailable. Stock trades still execute on the observed market day.
        benchmark_return = float((1 + benchmark_gross) * (1 - etf_sell) / (1 + buy) - 1) if len(benchmark_vals) else 0.
        benchmark_unexecutable+=int(benchmark_vals.empty)
        # Keep missing/unexecutable stock allocations in cash rather than
        # renormalizing survivors to hide suspensions or missing data.
        equal_weight_return = float((((1 + group.loc[valid, "trade_return_1"]) * (1 - sell)
                                      / (1 + buy) - 1).sum()) / len(group))
        orders += 2 * len(eligible)
        unexecutable += int((~valid).sum())
        hold_log = group.get("benchmark_target_1", pd.Series(dtype=float)).dropna()
        hold_return = float(np.expm1(hold_log.iloc[0])) if len(hold_log) else benchmark_gross
        absolute_open = group.get("benchmark_adjusted_next_open", pd.Series(dtype=float)).dropna()
        absolute_close = group.get("benchmark_end_adj_close_1", pd.Series(dtype=float)).dropna()
        rows.append({"date": pd.Timestamp(group.next_open_date.dropna().iloc[0]), "anchor_date": anchor,
                     "strategy_return": strategy_return,'strategy_gross_return':strategy_gross, "benchmark_return": benchmark_return,
                     "equal_weight_return": equal_weight_return, "benchmark_hold_return": hold_return,
                     "benchmark_intraday_gross": benchmark_gross,
                     "benchmark_absolute_open": float(absolute_open.iloc[0]) if len(absolute_open) else np.nan,
                     "benchmark_absolute_close": float(absolute_close.iloc[0]) if len(absolute_close) else np.nan,
                     "exposure": allocation / (1 + buy), "positions": len(eligible),
                     "turnover": float(weight * np.sum((2 + eligible.trade_return_1) / (1 + buy)))})
    equity = pd.DataFrame(rows)
    if equity.empty:
        return equity, {"status": "unavailable", "reason": "no executable dated observations", "orders": 0}
    for name in ["strategy", "benchmark", "equal_weight"]:
        equity[f"{name}_net"] = (1 + equity[f"{name}_return"]).cumprod()
    # Additional passive ETF reference: buy once at the first executable open,
    # hold through all following overnight sessions, liquidate at the last close.
    gaps = bool((equity.anchor_date.iloc[1:].to_numpy() != equity.date.iloc[:-1].to_numpy()).any())
    has_absolute_prices = bool(equity.benchmark_absolute_open.iloc[0]>0
                               and equity.benchmark_absolute_close.iloc[-1]>0)
    if has_absolute_prices:
        hold_equity = equity.benchmark_absolute_close.to_numpy() / equity.benchmark_absolute_open.iloc[0] / (1 + buy)
        hold_equity[-1] *= (1 - etf_sell)
        prior = np.r_[1., hold_equity[:-1]]
        hold_returns = hold_equity / prior - 1
        gaps = gaps or not np.isfinite(hold_equity).all()
    elif not gaps:
        hold_returns = equity.benchmark_hold_return.to_numpy(copy=True)
        hold_returns[0] = (1 + equity.benchmark_intraday_gross.iloc[0]) / (1 + buy) - 1
        hold_returns[-1] = (1 + hold_returns[-1]) * (1 - etf_sell) - 1
        hold_equity = np.cumprod(1 + hold_returns)
    else:
        hold_returns = np.full(len(equity), np.nan)
        hold_equity = np.full(len(equity), np.nan)
    equity["benchmark_buy_hold_net"] = hold_equity
    metrics = {"status": "ok", "orders": orders, "dates": len(equity),
               "benchmark_unexecutable_dates":benchmark_unexecutable,
               "unexecutable_stock_observations": unexecutable,
               "mean_exposure": float(equity.exposure.mean()), "turnover": float(equity.turnover.sum()),
               "execution": "next_open_to_same_day_close; flat overnight; no multi-day compounding",
               "costs": {"commission_per_side": config.commission, "slippage_per_side": config.slippage,
                         "stock_sell_tax": config.sell_tax, "etf_sell_tax": config.etf_sell_tax}}
    for name in ["strategy", "benchmark", "equal_weight"]:
        metrics[name] = _performance(equity[f"{name}_return"].to_numpy(), equity[f"{name}_net"].to_numpy())
    gross_nav=(1+equity.strategy_gross_return).cumprod()
    metrics['gross_strategy']=_performance(equity.strategy_gross_return.to_numpy(),gross_nav.to_numpy())
    metrics['cost_drag_percentage_points']=(metrics['gross_strategy']['total_return']-metrics['strategy']['total_return'])*100
    metrics['gross_selection']='identical_frozen_selected_trades'
    if np.isfinite(hold_equity).all():
        passive = _performance(hold_returns, hold_equity)
        passive["status"] = "total_return_only" if gaps else "ok"
        if gaps:
            # Endpoint prices recover the total return across omitted dates,
            # but daily risk and annualization need the unbroken daily series.
            passive.update(annualized_return=None, sharpe=None, max_drawdown=None,
                           reason="evaluation dates have gaps; endpoint total return is valid, daily risk metrics withheld")
        metrics["benchmark_buy_hold"] = passive
    elif has_absolute_prices and np.isfinite(hold_equity[-1]):
        metrics['benchmark_buy_hold']=dict(status='total_return_only',total_return=float(hold_equity[-1]-1),
            annualized_return=None,sharpe=None,max_drawdown=None,
            reason='missing benchmark valuations; exact endpoint total return retained, daily risk withheld')
    else:
        metrics["benchmark_buy_hold"] = {"status": "unavailable", "total_return": None,
            "annualized_return": None, "sharpe": None, "max_drawdown": None,
            "reason": "evaluation date gaps require absolute benchmark endpoint prices"}
    return equity, metrics


def run_research(frame: pd.DataFrame, feature_cols: list[str], config: ResearchConfig | None = None) -> ResearchResult:
    config = config or ResearchConfig()
    data = _validate(frame, feature_cols, config)
    evaluation=_validate(frame,feature_cols,config,training=False) if config.evaluation_tickers else pd.DataFrame()
    output, fold_rows, models,transfer = [], [], {},[]
    for horizon in config.horizons:
        for fold, split in enumerate(temporal_splits(data, horizon, config), start=1):
            train, calibration, test = (data.loc[split[k]] for k in ("train", "calibration", "test"))
            model = _fit(train, calibration, feature_cols, horizon, config)
            pred = model.predict(test)
            pred["fold"] = fold
            pred["actual_return"] = np.expm1(test[f"target_{horizon}"])
            pred["label_end_date"] = test[f"label_end_{horizon}"]
            for c in ["close", "adj_close", "next_open", "next_open_date", f"trade_return_{horizon}",
                      f"benchmark_trade_return_{horizon}", f"benchmark_target_{horizon}", f"trade_valid_{horizon}",
                      "benchmark_adj_close", "benchmark_adjusted_next_open", f"benchmark_end_adj_close_{horizon}"]:
                if c in test:
                    pred[c] = test[c]
            output.append(pred)
            if not evaluation.empty:
                external=evaluation[evaluation.date.isin(test.date.unique())]
                if not external.empty:
                    external_prediction=predict_evaluation(model,external,horizon)
                    external_prediction['fold']=fold
                    transfer.append(external_prediction)
            models[horizon] = model
            fold_rows.append({
                "horizon": horizon, "fold": fold, "model_version": model.model_version,
                "train_start": model.training_start, "train_end": train.date.max(),
                "train_label_end": train[f"label_end_{horizon}"].max(),
                "calibration_start": calibration.date.min(), "calibration_end": calibration.date.max(),
                "calibration_label_end": calibration[f"label_end_{horizon}"].max(),
                "test_start": test.date.min(), "test_end": test.date.max(),
                "train_dates": model.training_dates, "calibration_dates": calibration.date.nunique(),
                "test_dates": test.date.nunique(), "train_rows": model.training_rows, "test_rows": len(test),
            })
    predictions = pd.concat(output, ignore_index=True)
    by_horizon = {str(h): _horizon_metrics(g) for h, g in predictions.groupby("horizon")}
    equity, portfolio = simulate_portfolio(predictions, config)
    checks = {}
    for h, m in by_horizon.items():
        checks[f"h{h}_enough_out_of_sample_dates"] = m["test_dates"] >= 126 and m["folds"] >= 3
        checks[f"h{h}_brier_beats_baseline"] = m["brier_skill"] > 0
        checks[f'h{h}_beats_fixed_probability']=m.get('brier_skill_vs_p50',0)>0
        checks[f'h{h}_beats_training_frequency']=m.get('brier_skill_vs_frequency') is not None and m['brier_skill_vs_frequency']>0
        checks[f'h{h}_direction_not_one_sided']=m.get('balanced_accuracy') is not None and m['balanced_accuracy']>.5
        checks[f"h{h}_interval_coverage"] = m["coverage80"] is not None and .7 <= m["coverage80"] <= .95
    checks["net_beats_passive_0050"] = (portfolio.get("status") == "ok"
        and portfolio["benchmark_buy_hold"]["total_return"] is not None
        and portfolio["strategy"]["total_return"] > portfolio["benchmark_buy_hold"]["total_return"])
    checks["at_least_30_executed_orders"] = portfolio.get("orders", 0) >= 30
    metrics = {"by_horizon": by_horizon, "portfolio": portfolio,
               "feasibility": {"passed": all(checks.values()), "checks": checks,
                               "note": "Research screening only. Requires real PIT data, untouched holdout and paper tracking before promotion."},
               "model": "fixed 50/50 Ridge + histogram boosting; temporal calibration: " + config.calibration_policy,
               "prediction_reference": "last adjusted close; expected_return is exp(predicted log return)-1",
               "config": asdict(config)}
    transfer_frame=pd.concat(transfer,ignore_index=True) if transfer else pd.DataFrame()
    metrics['transfer']=transfer_metrics(transfer_frame)
    metrics['training_tickers']=list(TRAINING_TICKERS)
    metrics['objective']=config.objective
    return ResearchResult(predictions, metrics, pd.DataFrame(fold_rows), equity, models,transfer_frame)


def train_latest(frame: pd.DataFrame, feature_cols: list[str], config: ResearchConfig | None = None,
                 as_of: pd.Timestamp | str | None = None) -> dict[int, TrainedModel]:
    """Fit a frozen latest candidate, only using labels matured by as_of.

    Labels dated the cutoff day become eligible at 20:00 Taipei, the conservative
    daily-data availability convention. Training outcomes must also have been
    available before the calibration/embargo boundary, so an old price received
    during calibration cannot enter the frozen predictor. Callers must enforce
    the source's observed available_at for actual live usage.
    """
    config = config or ResearchConfig()
    cutoff = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now(tz="UTC")
    if cutoff.tzinfo is None:
        raise ValueError("as_of must contain an explicit timezone")
    cutoff = cutoff.tz_convert("UTC")
    data = _validate(frame, feature_cols, config)
    data = data.loc[data.decision_ts.le(cutoff)]
    models = {}
    for h in config.horizons:
        end = pd.to_datetime(data[f"label_end_{h}"])
        availability = (pd.to_datetime(data[f"label_available_at_{h}"], utc=True)
                        if f"label_available_at_{h}" in data else
                        (end.dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).dt.tz_convert("UTC"))
        mature = data.loc[data[f"target_{h}"].notna() & availability.le(cutoff)].copy()
        dates = pd.DatetimeIndex(mature.date.unique()).sort_values()
        required = config.min_train_dates + config.calibration_dates + h + config.embargo_dates
        if len(dates) < required:
            raise ValueError(f"insufficient mature history dates for latest horizon {h}: {len(dates)} < {required}")
        calibration_start = dates[-config.calibration_dates]
        train_stop = dates[-config.calibration_dates - config.embargo_dates] if config.embargo_dates else calibration_start
        training_availability_cutoff = train_stop.tz_localize("Asia/Taipei").tz_convert("UTC")
        train = mature.loc[(mature.date < train_stop) & (mature[f"label_end_{h}"] < train_stop)
                           & availability.reindex(mature.index).lt(training_availability_cutoff)]
        calibration = mature.loc[mature.date >= calibration_start]
        if train.date.nunique() < config.min_train_dates:
            raise ValueError(f"insufficient purged training history for horizon {h}")
        models[h] = _fit(train, calibration, feature_cols, h, config)
        models[h].training_cutoff = cutoff.isoformat()
    return models
