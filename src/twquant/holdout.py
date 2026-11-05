"""Frozen chronological holdout evaluation, independent of development scores.

The caller must reserve the holdout before development and seal its source,
configuration and dataset. This function cannot establish that a person or an
optimizer has never inspected those dates; its protocol reports that limitation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from .research import ResearchConfig, TrainedModel, _horizon_metrics, _validate, simulate_portfolio, train_latest


@dataclass
class HoldoutResult:
    predictions: pd.DataFrame
    metrics: dict[str, Any]
    models: dict[int, TrainedModel]
    cutoff: str
    protocol: dict[str, Any]
    equity: pd.DataFrame
    folds: pd.DataFrame


def _aware(values, name):
    if any(pd.Timestamp(value).tzinfo is None for value in values if not pd.isna(value)):
        raise ValueError(f"{name} requires explicit timezone offsets")
    return pd.to_datetime(values, utc=True)


def _label_availability(frame, horizon):
    column = f"label_available_at_{horizon}"
    if column in frame:
        return _aware(frame[column], column)
    end = pd.to_datetime(frame[f"label_end_{horizon}"])
    return (end.dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).dt.tz_convert("UTC")


def evaluate_holdout(frame: pd.DataFrame, feature_cols: list[str], config: ResearchConfig,
                     holdout_dates: int = 126) -> HoldoutResult:
    """Fit before the final reserved sessions, then evaluate one frozen model.

    The last ``holdout_dates`` benchmark sessions (or unique anchor dates when
    benchmark rows were omitted) define the reserved sample. Neither held-out
    labels nor their availability determine prediction membership. Only labels
    available strictly before the first held-out decision can enter fitting.
    Missing future prices remain missing outcomes rather than down labels.
    """
    if isinstance(holdout_dates, bool) or not isinstance(holdout_dates, int) or holdout_dates < 1:
        raise ValueError("holdout_dates must be a positive integer")
    if not feature_cols or any(not column.startswith("f_") for column in feature_cols):
        raise ValueError("Holdout feature columns must use the explicit f_ allowlist")
    data = _validate(frame, feature_cols, config)
    data["decision_ts"] = _aware(data.decision_ts, "decision_ts")
    source_dates = frame.loc[frame.ticker.astype(str) == "0050", "date"]
    if source_dates.empty:
        source_dates = frame.date
    calendar = pd.DatetimeIndex(pd.to_datetime(source_dates).dropna().unique()).sort_values()
    if len(calendar) <= holdout_dates:
        raise ValueError("insufficient history dates before the requested holdout")
    reserved = calendar[-holdout_dates:]
    start, end = pd.Timestamp(reserved[0]), pd.Timestamp(reserved[-1])
    test = data.loc[data.date.isin(reserved) & data.decision_ts.notna()].copy()
    if test.empty:
        raise ValueError("holdout contains no decision-eligible target observations")
    first_decision = test.decision_ts.min()
    freeze = first_decision - pd.Timedelta(nanoseconds=1)
    development = data.loc[(data.date < start) & data.decision_ts.notna() & data.decision_ts.le(freeze)].copy()
    if development.empty:
        raise ValueError("insufficient development history before holdout freeze")
    models = train_latest(development, feature_cols, config, as_of=freeze)
    # As-of maturity of evaluation outcomes is separate from training maturity.
    # Input feature frames normally end at the caller's available-data cutoff.
    evaluation_cutoff = data.decision_ts.max()
    if "available_at" in frame:
        price_receipts = _aware(frame.available_at, "available_at")
        if price_receipts.notna().any():
            evaluation_cutoff = max(evaluation_cutoff, price_receipts.max())
    output, folds = [], []
    max_label_availability = {}
    for horizon in config.horizons:
        model = models[horizon]
        prediction = model.predict(test)
        prediction["fold"] = 1
        labels = pd.to_numeric(test[f"target_{horizon}"], errors="coerce")
        availability = _label_availability(test, horizon)
        label_known = availability.notna() & availability.le(evaluation_cutoff)
        with np.errstate(over="ignore", invalid="ignore"):
            actual = np.expm1(labels)
        prediction["actual_return"] = actual.where(label_known & np.isfinite(actual))
        prediction["label_end_date"] = test[f"label_end_{horizon}"]
        prediction["label_available_at"] = availability
        for column in ["close", "adj_close", "next_open", "next_open_date", f"trade_return_{horizon}",
                       f"benchmark_trade_return_{horizon}", f"benchmark_target_{horizon}", f"trade_valid_{horizon}",
                       "benchmark_adj_close", "benchmark_adjusted_next_open", f"benchmark_end_adj_close_{horizon}"]:
            if column in test:
                prediction[column] = test[column]
        # Unavailable outcomes cannot be credited as executable returns either.
        if f"trade_return_{horizon}" in prediction:
            prediction[f"trade_return_{horizon}"] = prediction[f"trade_return_{horizon}"].where(prediction.actual_return.notna())
        if f"trade_valid_{horizon}" in prediction:
            prediction[f"trade_valid_{horizon}"] = prediction[f"trade_valid_{horizon}"].fillna(False) & prediction.actual_return.notna()
        output.append(prediction)
        known = _label_availability(development, horizon)
        mature = development.loc[development[f"target_{horizon}"].notna() & known.le(freeze)]
        max_known = known.loc[mature.index].max()
        max_label_availability[str(horizon)] = max_known.isoformat() if pd.notna(max_known) else None
        folds.append({"horizon": horizon, "fold": 1, "model_version": model.model_version,
                      "train_start": mature.date.min(), "train_end": pd.Timestamp(model.trained_through),
                      "calibration_label_end": pd.Timestamp(model.calibration_ends),
                      "train_label_available_at_max": max_known,
                      "fit_cutoff": freeze, "test_start": start, "test_end": end,
                      "test_dates": test.date.nunique(), "test_rows": len(test)})
    predictions = pd.concat(output, ignore_index=True).sort_values(["horizon", "date", "ticker"]).reset_index(drop=True)
    by_horizon = {str(horizon): _horizon_metrics(predictions.loc[predictions.horizon == horizon])
                  for horizon in config.horizons}
    equity, portfolio = simulate_portfolio(predictions, config)
    checks = {"at_least_126_reserved_sessions": len(reserved) >= 126}
    for horizon in config.horizons:
        metrics = by_horizon[str(horizon)]
        checks[f"h{horizon}_at_least_100_valid_dates"] = metrics["test_dates"] >= 100
        checks[f"h{horizon}_brier_not_worse_than_baseline"] = (metrics["observations"] > 0
            and metrics["brier"] is not None and metrics["baseline_brier"] is not None
            and metrics["brier_skill"] >= 0 and metrics["brier"] <= metrics["baseline_brier"])
        coverage = metrics["coverage80"]
        checks[f"h{horizon}_interval_coverage"] = coverage is not None and .7 <= coverage <= .95
    passive = portfolio.get("benchmark_buy_hold", {}).get("total_return")
    strategy = portfolio.get("strategy", {}).get("total_return")
    checks["net_beats_passive_0050"] = (portfolio.get("status") == "ok" and passive is not None
                                          and strategy is not None and strategy > passive)
    checks["at_least_30_executed_orders"] = portfolio.get("orders", 0) >= 30
    protocol = {
        "holdout_start": start.date().isoformat(), "holdout_end": end.date().isoformat(),
        "holdout_dates": len(reserved), "decision_eligible_dates": int(test.date.nunique()),
        "first_test_decision": first_decision.isoformat(), "fit_cutoff": freeze.isoformat(),
        "evaluation_cutoff": evaluation_cutoff.isoformat(),
        "max_training_label_available_at": max_label_availability,
        "untouched_verified": False, "development_exclusion_required": True,
        "caller_requirements": "Reserve these dates before development; freeze configuration, source SHA and dataset snapshot in a seal before exposing results. Evaluate the sealed copy once; changing the design requires a fresh future holdout.",
        "limitation": "This function is a retrospective chronological audit unless the caller supplies an independently enforced reservation and immutable seal; it cannot certify that held-out outcomes were never previously inspected.",
        "label_availability": "Observed label_available_at_<h> when supplied, otherwise reconstructed 20:00 Asia/Taipei; reconstructed timestamps do not establish real PIT history.",
        "config_sha256": hashlib.sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest(),
        "feature_columns": list(feature_cols),
    }
    metrics = {"by_horizon": by_horizon, "portfolio": portfolio,
               "feasibility": {"passed": all(checks.values()), "checks": checks,
                               "note": "Statistical screening only. Untouched status requires the caller's sealed protocol; data provenance and live paper evidence are separate gates."},
               "config": asdict(config), "sample": "frozen_final_holdout"}
    return HoldoutResult(predictions, metrics, models, freeze.isoformat(), protocol, equity, pd.DataFrame(folds))
