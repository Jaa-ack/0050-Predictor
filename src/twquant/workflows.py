"""Local research and daily paper-forecast orchestration.

Saved models are trusted LOCAL joblib artifacts. Never accept uploads as model files.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import joblib
import numpy as np
import pandas as pd

from .ledger import ForecastLedger, json_safe, utc_timestamp

from .universe import TRAINING_TICKERS, asset_class, evaluation_role, tickers as validate_tickers

TARGETS = set(TRAINING_TICKERS)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def ledger_for(root):
    return ForecastLedger(Path(root) / "forecasts.sqlite")


def load_events(path):
    if not path:
        return None
    return pd.read_csv(path, dtype={"ticker": str})


def data_kind(prices):
    sources = set(prices.get("source", pd.Series("unknown", index=prices.index)).astype(str).str.lower())
    return "synthetic" if any("synthetic" in s or "demo" in s or s == "test" for s in sources) else "historical"


def assess_readiness(prices, metrics, monitoring=None):
    """A conservative release gate; a green unit test is never investment evidence."""
    blockers = []
    if data_kind(prices) == "synthetic":
        blockers.append("synthetic_data")
    prices = prices.loc[prices.ticker.isin(TARGETS | {"0050"})]
    counts = prices.groupby("ticker")["date"].nunique()
    if not (TARGETS | {"0050"}) <= set(counts.index):
        blockers.append("required_target_universe_missing")
    benchmark_dates = pd.to_datetime(prices.loc[prices.ticker == "0050", "date"])
    benchmark_span = (benchmark_dates.max() - benchmark_dates.min()).days if len(benchmark_dates) else 0
    if len(counts) < 11 or counts.min() < 504 or counts.get("0050", 0) < 2000 or benchmark_span < 3650:
        blockers.append("ten_year_history_or_target_coverage_insufficient")
    basis = set(prices.get("availability_basis", pd.Series("unverified", index=prices.index)))
    if not basis or not basis <= {"observed", "verified_publication"}:
        blockers.append("point_in_time_history_unverified")
    adjustments = prices.get("adjustment_source", pd.Series("unverified", index=prices.index)).astype(str)
    if adjustments.str.contains("unverified|unknown", case=False).any():
        blockers.append("corporate_action_adjustments_unverified")
    passed = metrics.get("feasibility", {}).get("passed", False)
    if not passed:
        blockers.append("out_of_sample_edge_not_demonstrated")
    if not metrics.get("sealed_holdout", {}).get("passed", False):
        blockers.append("untouched_final_holdout_pending")
    if metrics.get('data_quality',{}).get('external_history_verified') is False:
        blockers.append('external_history_time_unverified')
    if metrics.get('data_quality',{}).get('synthetic_external'):
        blockers.append('synthetic_external_data')
    monitor = monitoring or {}
    h1 = monitor.get("by_horizon", {}).get("1", {})
    if h1.get("count", 0) < 500 or monitor.get("settled_decision_dates", 0) < 60:
        blockers.append("paper_track_record_insufficient")
    elif (h1.get("brier") is None or h1["brier"] >= .25 or h1.get("coverage") is None
          or not .65 <= h1["coverage"] <= .95):
        blockers.append("paper_prediction_quality_insufficient")
    return dict(passed=not blockers, blockers=blockers, data_kind=data_kind(prices),
                scope="fixed_current_universe_research", historical_sessions_min=int(counts.min()) if len(counts) else 0,
                requirement="Real PIT history, benchmark >=10 calendar years / 2000 sessions, each target >=504 sessions, sealed holdout, OOS edge and >=60 paper dates / 500 timely matured h1 forecasts.",
                limitations=["Current-universe selection has survivorship bias.",
                             "Historical index-membership proof is outside the fixed-current-universe scope.",
                             "Passing this gate allows tracked forecasts; it does not authorize trading."])


def source_digest():
    h = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def research(root, config=None, as_of=None, events_path=None, mode="postclose", holdout_dates=126):
    from .data import PriceStore
    from .features import build_features, select_feature_columns
    from .research import ResearchConfig, run_research, train_latest

    root = Path(root)
    config = config or ResearchConfig()
    prices = PriceStore(root / "data").read_prices()
    snapshot_id = prices.attrs.get("snapshot_id") or PriceStore(root / "data").latest_snapshot_id()
    if as_of is None:
        as_of = pd.to_datetime(prices.available_at, utc=True).max()
    cutoff = utc_timestamp(as_of)
    prices = prices[pd.to_datetime(prices.available_at, utc=True) <= cutoff].copy()
    if prices.empty:
        raise ValueError("No price data available at the requested cutoff")
    events = load_events(events_path)
    frame = build_features(prices, external=events, mode=mode)
    frame = frame[frame.decision_ts.notna() & (pd.to_datetime(frame.decision_ts, utc=True) <= cutoff)].copy()
    cols = select_feature_columns(frame, config.feature_profile)
    from .comparison import reserve_development
    development, holdout_start = reserve_development(frame, config.horizons, holdout_dates)
    dates = sorted(frame.loc[frame.ticker == "0050", "date"].unique())
    first_holdout_decision = pd.to_datetime(frame.loc[frame.date == holdout_start, "decision_ts"], utc=True).min()
    external_hash = hashlib.sha256(Path(events_path).read_bytes()).hexdigest() if events_path else None
    external_basis=sorted(events.availability_basis.dropna().astype(str).unique().tolist()) if events is not None and 'availability_basis' in events else []
    external_synthetic=bool(events is not None and 'data_kind' in events and
        events.data_kind.astype(str).str.contains('synthetic|demo',case=False).any())
    provenance = dict(snapshot_id=snapshot_id, config=asdict(config), cutoff=cutoff.isoformat(),
                      external_sha256=external_hash, source_sha256=source_digest(), mode=mode,
                      holdout_dates=holdout_dates, holdout_start=str(holdout_start.date()))
    policy = dict(config=asdict(config), source_sha256=provenance["source_sha256"], mode=mode, features=cols,
                  holdout_dates=holdout_dates, data_kind=data_kind(prices),
                  availability_basis=sorted(prices.availability_basis.unique().tolist()),
                  adjustment_sources=sorted(prices.adjustment_source.unique().tolist()),
                  external_basis=external_basis,synthetic_external=external_synthetic)
    provenance["pipeline_version"] = hashlib.sha256(json.dumps(json_safe(policy), sort_keys=True).encode()).hexdigest()[:24]
    run_id = hashlib.sha256(json.dumps(json_safe(provenance), sort_keys=True).encode()).hexdigest()[:20]
    folder = root / "runs" / run_id
    if (folder / "report.json").exists():
        write_json(root / "current.json", dict(run_id=run_id))
        return json.loads((folder / "report.json").read_text())
    result = run_research(development, cols, config)
    from .diagnostics import paired_block_audit
    result.metrics["paired_loss_audit"] = paired_block_audit(result.predictions)
    result.metrics['data_quality']=dict(synthetic_external=external_synthetic,
        external_history_verified=events is None or bool(external_basis) and
            set(external_basis)<={'observed','verified_publication','vintage_day_conservative'} and not external_synthetic,
        external_availability_basis=external_basis,
        external_coverage={c:float(development[c].notna().mean()) for c in cols if c.startswith('f_ext_')},
        note='Current financial snapshots cannot establish historical announcement/revision vintages; inspect coverage before adding factors.')
    registry_path = root / "validation_registry.json"
    if registry_path.exists():
        evidence = json.loads(registry_path.read_text()).get(provenance["pipeline_version"])
        if evidence and evidence.get("data_kind") == "historical":
            verified_at = utc_timestamp(evidence["validated_through"])
            if verified_at <= cutoff <= verified_at + pd.Timedelta(days=90):
                result.metrics["sealed_holdout"] = evidence
    # Fresh production fit uses matured labels and a distinct temporal calibration tail.
    models = train_latest(frame, cols, config, as_of=cutoff)
    monitoring = ledger_for(root).monitoring(pipeline_version=provenance["pipeline_version"], eligible_only=True)
    readiness = assess_readiness(prices, result.metrics, monitoring)
    report = dict(run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(),
                  provenance=provenance, data_kind=data_kind(prices), readiness=readiness,
                  metrics=result.metrics, n_prices=len(prices), n_features=len(cols),
                  date_range=[str(prices.date.min().date()), str(prices.date.max().date())],
                  python=sys.version.split()[0],
                  training_tickers=list(TRAINING_TICKERS), evaluation_tickers=list(config.evaluation_tickers),
                  scope="Fixed user-selected ten train the shared model. Other assets are same-model transfer tests only.",
                  interval_note="Calibrated 80% residual interval; serial dependence means coverage is empirical, not guaranteed.")
    folder.mkdir(parents=True, exist_ok=True)
    development.to_parquet(folder / "features.parquet", index=False)
    if events_path:
        (folder / "external_events.csv").write_bytes(Path(events_path).read_bytes())
    write_json(folder / "seal.json", dict(run_id=run_id, provenance=provenance,
        holdout_start=str(holdout_start.date()), holdout_end=str(pd.Timestamp(dates[-1]).date()),
        first_holdout_decision=first_holdout_decision.isoformat(),
        development_rows=len(development), opened=False,
        policy="Freeze configuration/source/snapshot before opening; never tune on this holdout. Production refit uses all matured data separately."))
    result.predictions.to_parquet(folder / "oos_predictions.parquet", index=False)
    result.transfer_predictions.to_parquet(folder / "transfer_oos_predictions.parquet", index=False)
    result.folds.to_csv(folder / "folds.csv", index=False)
    result.equity.to_csv(folder / "equity.csv", index=False)
    training_frame = frame.loc[frame.ticker.isin(TARGETS)]
    feature_reference = {c:dict(mean=float(training_frame[c].mean()), std=float(training_frame[c].std())) for c in cols}
    bundle = dict(models=models, feature_columns=cols, fit_cutoff=cutoff.isoformat(),
                  run_id=run_id, feature_reference=feature_reference, mode=mode, data_kind=data_kind(prices),
                  provenance=provenance)
    joblib.dump(bundle, folder / "models.joblib")
    report["model_sha256"] = hashlib.sha256((folder / "models.joblib").read_bytes()).hexdigest()
    write_json(folder / "report.json", report)
    write_json(root / "current.json", dict(run_id=run_id))
    return report


def current_report(root, champion=False):
    root = Path(root)
    pointer = root / ("champion.json" if champion else "current.json")
    if not pointer.exists():
        raise ValueError("No promoted model available; run research and use --paper for shadow validation" if champion
                         else "No trained model; run research first")
    run_id = json.loads(pointer.read_text())["run_id"]
    report = json.loads((root / "runs" / run_id / "report.json").read_text())
    holdout = root / "runs" / run_id / "holdout_report.json"
    if holdout.exists():
        result = json.loads(holdout.read_text())
        report["metrics"]["sealed_holdout"] = dict(passed=result["metrics"]["feasibility"]["passed"],
            metrics=result["metrics"], protocol=result["protocol"])
        from .data import PriceStore
        p = PriceStore(root / "data").read_prices(report["provenance"]["snapshot_id"])
        p = p[pd.to_datetime(p.available_at, utc=True) <= utc_timestamp(report["provenance"]["cutoff"])]
        monitor = ledger_for(root).monitoring(pipeline_version=report["provenance"]["pipeline_version"], eligible_only=True)
        report["readiness"] = assess_readiness(p, report["metrics"], monitor)
    return report


def release_holdout(root):
    """Open a sealed, frozen final audit once; repeated reads return its record."""
    from .data import PriceStore
    from .features import build_features, select_feature_columns
    from .holdout import evaluate_holdout
    from .research import ResearchConfig
    root = Path(root)
    report = current_report(root)
    folder = root / "runs" / report["run_id"]
    target = folder / "holdout_report.json"
    if target.exists():
        return json.loads(target.read_text())
    if report["provenance"]["source_sha256"] != source_digest():
        raise ValueError("Code changed since seal; recreate a development run with a new final holdout protocol")
    seal = json.loads((folder / "seal.json").read_text())
    p = PriceStore(root / "data").read_prices(report["provenance"]["snapshot_id"])
    cutoff = utc_timestamp(report["provenance"]["cutoff"])
    p = p[pd.to_datetime(p.available_at, utc=True) <= cutoff]
    events_path = folder / "external_events.csv"
    events = load_events(events_path) if events_path.exists() else None
    if events_path.exists() and hashlib.sha256(events_path.read_bytes()).hexdigest() != report["provenance"]["external_sha256"]:
        raise ValueError("Sealed external inputs changed")
    frame = build_features(p, external=events, mode=report["provenance"]["mode"])
    frame = frame[frame.decision_ts.notna() & (pd.to_datetime(frame.decision_ts, utc=True) <= cutoff)].copy()
    values = dict(report["provenance"]["config"])
    values["horizons"] = tuple(values["horizons"])
    values["evaluation_tickers"] = tuple(values.get("evaluation_tickers", []))
    config = ResearchConfig(**values)
    result = evaluate_holdout(frame, select_feature_columns(frame, config.feature_profile), config,
                              holdout_dates=report["provenance"]["holdout_dates"])
    from .diagnostics import paired_block_audit
    result.metrics["paired_loss_audit"] = paired_block_audit(result.predictions)
    result.protocol.update(workflow_sealed=True, seal_sha256=hashlib.sha256((folder / "seal.json").read_bytes()).hexdigest(),
                           run_id=report["run_id"], note="Code/config/snapshot frozen before opening. Human reuse across research runs still requires discipline.")
    result.predictions.to_parquet(folder / "holdout_predictions.parquet", index=False)
    result.transfer_predictions.to_parquet(folder / "transfer_holdout_predictions.parquet", index=False)
    result.equity.to_csv(folder / "holdout_equity.csv", index=False)
    payload = dict(metrics=result.metrics, protocol=result.protocol, cutoff=result.cutoff,
                   opened_at=datetime.now(timezone.utc).isoformat(), data_kind=report["data_kind"])
    write_json(target, payload)
    quality = assess_readiness(p, dict(feasibility=dict(passed=True), sealed_holdout=dict(passed=True),
        data_quality=report['metrics'].get('data_quality',{})), {})
    provenance_blockers = set(quality["blockers"]) - {"paper_track_record_insufficient", "paper_prediction_quality_insufficient"}
    if report["data_kind"] == "historical" and not provenance_blockers and result.metrics["feasibility"]["passed"]:
        path = root / "validation_registry.json"
        registry = json.loads(path.read_text()) if path.exists() else {}
        registry[report["provenance"]["pipeline_version"]] = dict(passed=True,
            validation_run_id=report["run_id"], validated_through=report["provenance"]["cutoff"],
            data_kind="historical", metrics=result.metrics, protocol=result.protocol)
        write_json(path, registry)
    return payload


def promote(root):
    """Promote a checked candidate for forecasts only. There is no broker integration."""
    from .data import PriceStore
    root = Path(root)
    report = current_report(root)
    candidate_prices = PriceStore(root / "data").read_prices(snapshot_id=report["provenance"]["snapshot_id"])
    cutoff = utc_timestamp(report["provenance"]["cutoff"])
    candidate_prices = candidate_prices[pd.to_datetime(candidate_prices.available_at, utc=True) <= cutoff]
    # Paper evidence must concern this real model family and decision mode.
    monitor = ledger_for(root).monitoring(pipeline_version=report["provenance"]["pipeline_version"], eligible_only=True)
    gate = assess_readiness(candidate_prices, report["metrics"], monitor)
    if report["data_kind"] != "historical" and "synthetic_data" not in gate["blockers"]:
        gate["blockers"].append("synthetic_data")
        gate["passed"] = False
    if not gate["passed"]:
        raise ValueError("Promotion blocked: " + ", ".join(gate["blockers"]))
    write_json(root / "champion.json", dict(run_id=report["run_id"], readiness=gate,
                                           promoted_at=datetime.now(timezone.utc).isoformat()))
    return gate


def valuation_at(path, ticker, as_of):
    from .valuation import value_company
    if not path:
        return dict(status="insufficient_data", reason="No point-in-time fundamental inputs supplied")
    df = pd.read_csv(path, dtype={"ticker": str})
    required = {"ticker", "sector", "available_at", "published_at"}
    if not required <= set(df):
        raise ValueError("Fundamentals require ticker, sector, published_at, available_at")
    a = pd.to_datetime(df.available_at, utc=True)
    p = pd.to_datetime(df.published_at, utc=True)
    if (p > a).any():
        raise ValueError("Fundamentals published_at exceeds available_at")
    rows = df[(df.ticker == ticker) & (a <= as_of)].copy()
    if rows.empty:
        return dict(status="insufficient_data", reason="No financial report known at cutoff")
    rows["_available"] = pd.to_datetime(rows.available_at, utc=True)
    row = rows.sort_values("_available").iloc[-1].dropna().to_dict()
    result = value_company(row, sector=row["sector"])
    result["inputs_available_at"] = row["available_at"]
    return result


def forecast(root, as_of, paper=False, events_path=None, fundamentals_path=None, max_stale_days=5, tickers=None):
    from .data import PriceStore
    from .features import build_features, point_in_time_join
    root = Path(root)
    cutoff = utc_timestamp(as_of)
    if cutoff > pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=1):
        raise ValueError("Prediction cutoff cannot be in the future")
    report = current_report(root, champion=not paper)
    selected = set(validate_tickers(tickers)) if tickers is not None else TARGETS
    if not paper and not selected <= TARGETS:
        raise ValueError("Transfer assets require --paper; core approval cannot authorize transfer forecasts")
    if report["provenance"]["config"].get("objective", "close_return") != "close_return":
        raise ValueError("Price forecasts require a close_return objective; open_return models are execution research only")
    folder = root / "runs" / report["run_id"]
    if source_digest() != report["provenance"]["source_sha256"]:
        raise ValueError("Code changed since model training; retrain before issuing forecasts")
    if hashlib.sha256((folder / "models.joblib").read_bytes()).hexdigest() != report["model_sha256"]:
        raise ValueError("Model artifact checksum mismatch")
    bundle = joblib.load(folder / "models.joblib")
    if utc_timestamp(bundle["fit_cutoff"]) > cutoff:
        raise ValueError("Model was fitted after the requested prediction cutoff; train a historical vintage first")
    prices = PriceStore(root / "data").read_prices()
    snapshot_id = prices.attrs.get("snapshot_id") or PriceStore(root / "data").latest_snapshot_id()
    if data_kind(prices) != bundle["data_kind"]:
        raise ValueError("Synthetic and real datasets cannot share models or forecast ledgers")
    p = prices[pd.to_datetime(prices.available_at, utc=True) <= cutoff].copy()
    if p.empty:
        raise ValueError("No completed price data at cutoff")
    last_market = p.loc[p.ticker == "0050", "date"].max()
    if pd.isna(last_market):
        raise ValueError("0050 benchmark missing")
    age_days = (cutoff.tz_convert("Asia/Taipei").date() - last_market.date()).days
    if age_days > max_stale_days:
        raise ValueError(f"Price feed stale ({age_days} calendar days); forecast withheld")
    context = set(p.loc[p.date.eq(last_market) & p.ticker.isin(TARGETS), "ticker"])
    if context != TARGETS:
        raise ValueError("Fixed training universe context incomplete: " + ", ".join(sorted(TARGETS-context)))
    events = load_events(events_path)
    if bundle["provenance"].get("external_sha256") and events is None:
        raise ValueError("This model requires the same external feature feed used in training")
    frame = build_features(p, external=events, mode=bundle["mode"])
    latest = frame[(frame.date == last_market) & frame.ticker.isin(selected)].copy()
    missing_targets = selected - set(latest.ticker)
    if missing_targets:
        raise ValueError("Missing or stale latest target prices: " + ", ".join(sorted(missing_targets)))
    if latest.empty:
        raise ValueError("No target stock with current completed price data")
    # Inference at 08:30 can use only prior completed prices. The historical model
    # must also be trained in preopen mode before treating this as an 08:30 model.
    local = cutoff.tz_convert("Asia/Taipei")
    if bundle["mode"] == "postclose" and (local.hour < 20 or local.date() != last_market.date()):
        raise ValueError("Postclose model requires same-session cutoff at or after 20:00 Asia/Taipei")
    if bundle["mode"] == "preopen" and (local.hour != 8 or local.minute < 30 or local.date() <= last_market.date()):
        raise ValueError("Preopen model requires 08:30-08:59 Asia/Taipei after the latest completed session")
    if bundle["mode"] == "preopen":
        calendar_path = root / "session_calendar.csv"
        # A historical snapshot can establish historical exchange sessions;
        # future/live sessions must be supplied from a verified exchange calendar.
        verified = set(pd.to_datetime(prices.loc[prices.ticker == "0050", "date"]).dt.date)
        if calendar_path.exists():
            calendar = pd.read_csv(calendar_path, dtype={"date":str})
            if not {"date", "is_open", "source"} <= set(calendar):
                raise ValueError("Calendar requires date,is_open,source")
            verified |= set(pd.to_datetime(calendar.loc[calendar.is_open.astype(str).isin(["1","True","true"]), "date"]).dt.date)
        if local.date() not in verified:
            raise ValueError("Preopen session is unverified; import an exchange session_calendar.csv")
    latest["decision_ts"] = cutoff
    ext_columns = [c for c in latest if c.startswith("f_ext_") or c.startswith("f_age_")]
    latest = latest.drop(columns=ext_columns)
    if events is not None:
        latest = point_in_time_join(latest, events)
    missing = set(bundle["feature_columns"]) - set(latest)
    if missing:
        raise ValueError("Inference feature schema missing: " + ", ".join(sorted(missing)))
    required_external = [c for c in bundle["feature_columns"] if c.startswith("f_ext_") and not c.endswith("_age_days")]
    stale_external = pd.Series(False, index=latest.index)
    for col in required_external:
        stale_external |= latest[col].isna()
        age_col = col + "_age_days"
        if age_col in latest:
            stale_external |= latest[age_col] > 120
    records = []
    max_z = pd.Series(0.0, index=latest.index)
    for c, ref in bundle["feature_reference"].items():
        if ref["std"] and np.isfinite(ref["std"]) and ref["std"] > 1e-9:
            z = abs((latest[c] - ref["mean"]) / ref["std"])
            max_z = pd.concat([max_z, z.where(np.isfinite(z), 0.0)], axis=1).max(axis=1)
    for horizon, model in bundle["models"].items():
        outputs = model.predict(latest).reset_index(drop=True)
        for i, (_, row) in enumerate(latest.iterrows()):
            out = outputs.iloc[i].to_dict()
            probability = float(out["probability_up"])
            expected = float(out["expected_return"])
            cost = 2 * report["provenance"]["config"]["commission"] + report["provenance"]["config"]["sell_tax"] + 2 * report["provenance"]["config"]["slippage"]
            reason = "research_only" if paper else "tracked_forecast"
            signal = "observe" if probability >= .55 and expected > cost else "no_edge"
            row_z = float(max_z.loc[row.name])
            if row_z > 5:
                signal = "abstain"
                reason = "feature_distribution_shift"
            if stale_external.loc[row.name]:
                signal = "abstain"
                reason = "external_features_missing_or_stale"
            records.append(dict(ticker=str(row.ticker), anchor_date=str(row.date.date()),
                decision_ts=cutoff.isoformat(), data_cutoff=cutoff.isoformat(), horizon=int(horizon),
                model_version=f"{report['run_id']}-{model.model_version}", snapshot_id=snapshot_id,
                probability_up=probability, expected_return=expected,
                arithmetic_expected_return=float(out["arithmetic_expected_return"]),
                training_tickers=list(TRAINING_TICKERS), evaluation_role=evaluation_role(str(row.ticker)),
                asset_class=asset_class(row.ticker), tradable=asset_class(row.ticker) != "index",
                return_basis="price_index" if asset_class(row.ticker) == "index" else "corporate_action_adjusted",
                baseline_probability=float(out["baseline_probability"]),
                q10=float(out["q10"]), q50=float(out["q50"]), q90=float(out["q90"]),
                price_low=float(row.close * (1 + out["q10"])),
                price_median=float(row.close * (1 + out["q50"])),
                price_high=float(row.close * (1 + out["q90"])),
                price_basis="Price-index points; excludes dividend reinvestment" if asset_class(row.ticker) == "index" else "Anchor raw-share basis; future corporate actions may change quoted price units",
                reference_close=float(row.close), reference_adj_close=float(row.adj_close),
                status=reason, signal=signal, max_feature_z=row_z, data_kind=bundle["data_kind"],
                pipeline_version=bundle["provenance"]["pipeline_version"], mode=bundle["mode"],
                feature_version=bundle["provenance"]["source_sha256"],
                features={c:row[c] for c in bundle["feature_columns"]},
                valuation=dict(status="not_applicable", reason="ETF/index has no single-company valuation") if asset_class(row.ticker) != "equity" else valuation_at(fundamentals_path, str(row.ticker), cutoff),
                explanation=out.get("explanations", out.get("explanation", []))))
    inserted = ledger_for(root).append(records)
    write_json(root / "latest_forecast.json", dict(as_of=cutoff.isoformat(), inserted=inserted,
                                                  data_kind=bundle["data_kind"], forecasts=records))
    return dict(inserted=inserted, total=len(records), forecasts=records)


def settle(root, as_of):
    from .data import PriceStore
    store = PriceStore(Path(root) / "data")
    p = store.read_prices()
    n = ledger_for(root).settle(p, as_of, store.latest_snapshot_id())
    monitor = ledger_for(root).monitoring()
    write_json(Path(root) / "monitoring.json", monitor)
    return dict(newly_settled=n, monitoring=monitor)
