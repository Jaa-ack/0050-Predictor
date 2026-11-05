"""Immutable forecast/maturity ledger with trusted issuance and replay separation."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import sqlite3

import numpy as np
import pandas as pd

from .universe import TRAINING_TICKERS

_ISSUE_METADATA = {"issued_at", "is_replay"}
_REPLAY_TOLERANCE_SECONDS = 15 * 60


def utc_timestamp(value):
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        raise ValueError("timestamp requires an explicit timezone")
    return value.tz_convert("UTC")


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _without_issue_metadata(record):
    return {key: value for key, value in record.items() if key not in _ISSUE_METADATA}


def _price_data_kind(prices):
    if "source" not in prices or prices.source.isna().any():
        return "unknown"
    sources = set(prices.source.astype(str).str.lower())
    if not sources or "" in sources or "unknown" in sources:
        return "unknown"
    kinds = {"synthetic" if "synthetic" in source or source in {"demo", "test", "fake"}
             else "historical" for source in sources}
    if len(kinds) != 1:
        raise ValueError("Settlement price source mixes synthetic and historical data")
    return kinds.pop()


def _eligible_for_paper_record(record):
    # Transfer assets cannot supply evidence for the fixed-ten release gate.
    if record.get("ticker") not in TRAINING_TICKERS:
        return False
    # Old ledgers without system issuance fields cannot prove a timely forecast.
    if record.get("data_kind") != "historical" or record.get("is_replay") is not False:
        return False
    if not record.get("issued_at"):
        return False
    difference = abs((utc_timestamp(record["issued_at"]) - utc_timestamp(record["decision_ts"])).total_seconds())
    return difference <= _REPLAY_TOLERANCE_SECONDS


class ForecastLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS forecasts (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, decision_ts TEXT NOT NULL,
                    horizon INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes (
                    forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
                    payload TEXT NOT NULL);
            """)

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def append(self, forecasts):
        """Append immutable predictions; issue time and replay status are server-set.

        Caller-supplied issuance fields are ignored. Repeated identical inputs
        retain their original issue time and never create another observation.
        """
        inserted = 0
        with self._connect() as con:
            for record in forecasts:
                record = _without_issue_metadata(json_safe(dict(record)))
                record["decision_ts"] = utc_timestamp(record["decision_ts"]).isoformat()
                if "data_cutoff" in record:
                    record["data_cutoff"] = utc_timestamp(record["data_cutoff"]).isoformat()
                    if utc_timestamp(record["data_cutoff"]) > utc_timestamp(record["decision_ts"]):
                        raise ValueError("data cutoff exceeds decision timestamp")
                if int(record["horizon"]) not in (1, 5, 20):
                    raise ValueError("horizon must be 1, 5 or 20 trading sessions")
                nums = [record[k] for k in ("probability_up", "expected_return", "q10", "q50", "q90")]
                if any(v is None or not math.isfinite(v) for v in nums):
                    raise ValueError("forecast outputs must be finite")
                if not 0 <= record["probability_up"] <= 1:
                    raise ValueError("invalid probability")
                if not -1 < record["q10"] <= record["q50"] <= record["q90"]:
                    raise ValueError("invalid return interval")
                identity = [record[k] for k in ("ticker", "decision_ts", "horizon", "model_version")]
                fid = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                record["id"] = fid
                old = con.execute("SELECT payload FROM forecasts WHERE id=?", (fid,)).fetchone()
                if old:
                    if _without_issue_metadata(json.loads(old[0])) != record:
                        raise ValueError("forecast is immutable; use a new model version or decision timestamp")
                    continue
                issued_at = datetime.now(timezone.utc)
                record["issued_at"] = issued_at.isoformat()
                record["is_replay"] = abs((utc_timestamp(issued_at) - utc_timestamp(record["decision_ts"])).total_seconds()) > _REPLAY_TOLERANCE_SECONDS
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
                con.execute("INSERT INTO forecasts VALUES (?,?,?,?,?)",
                            (fid, record["ticker"], record["decision_ts"], record["horizon"], payload))
                inserted += 1
        return inserted

    def forecasts(self):
        with self._connect() as con:
            rows = con.execute("""SELECT f.payload,o.payload FROM forecasts f
                LEFT JOIN outcomes o ON o.forecast_id=f.id ORDER BY f.decision_ts DESC,f.ticker,f.horizon""").fetchall()
        return [dict(json.loads(p), **(json.loads(o) if o else {})) for p, o in rows]

    def settle(self, prices, as_of, snapshot_id):
        """Settle only later, available target observations from a compatible source.

        All source/time checks finish before any outcome is inserted. Historical
        replays may exercise settlement but never contribute to eligible paper
        monitoring. Both return endpoints use one adjustment vintage.
        """
        cutoff = utc_timestamp(as_of)
        pending = [f for f in self.forecasts() if "actual_return" not in f]
        price_kind = _price_data_kind(prices)
        for record in pending:
            if record.get("data_kind") is not None and record["data_kind"] != price_kind:
                raise ValueError("Settlement data kind/source does not match forecast data kind")
        p = prices.copy()
        p["date"] = pd.to_datetime(p["date"]).dt.normalize()
        p["available_at"] = pd.to_datetime(p["available_at"], utc=True)
        p = p[p.available_at <= cutoff]
        if p.duplicated(["ticker", "date"]).any():
            raise ValueError("Settlement prices contain duplicate ticker/date rows")
        from .features import observed_market_sessions
        sessions = observed_market_sessions(p)
        lookup = p.set_index(["ticker", "date"])
        planned = []
        for record in pending:
            decision = utc_timestamp(record["decision_ts"])
            if decision > cutoff:
                continue
            anchor = pd.Timestamp(record["anchor_date"]).normalize()
            if anchor.date() > decision.tz_convert("Asia/Taipei").date():
                raise ValueError("Forecast anchor date is later than its decision date")
            future = [d for d in sessions if d > anchor]
            if len(future) < record["horizon"]:
                continue
            end = pd.Timestamp(future[record["horizon"] - 1])
            if (record["ticker"], anchor) not in lookup.index or (record["ticker"], end) not in lookup.index:
                continue
            endpoint = lookup.loc[(record["ticker"], end)]
            if utc_timestamp(endpoint["available_at"]) <= decision:
                raise ValueError("Forecast target outcome was already known at its decision timestamp")
            start_price = float(lookup.loc[(record["ticker"], anchor), "adj_close"])
            end_price = float(endpoint["adj_close"])
            if not all(math.isfinite(value) and value > 0 for value in (start_price, end_price)):
                raise ValueError("Settlement adjusted prices must be finite and positive")
            actual = end_price / start_price - 1
            outcome = dict(actual_return=actual, label_end_date=end.date().isoformat(),
                           settled_at=cutoff.isoformat(), outcome_snapshot_id=snapshot_id,
                           brier=(record["probability_up"] - float(actual > 0)) ** 2,
                           absolute_error=abs(record["expected_return"] - actual),
                           covered=bool(record["q10"] <= actual <= record["q90"]))
            planned.append((record["id"], json.dumps(outcome, allow_nan=False)))
        inserted = 0
        with self._connect() as con:
            for forecast_id, payload in planned:
                cur = con.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?)", (forecast_id, payload))
                inserted += cur.rowcount
        return inserted

    def monitoring(self, run_id=None, pipeline_version=None, eligible_only=False):
        """Summarize all forecasts, or timely real forecasts for a release policy.

        Supplying a run/pipeline filter always excludes synthetic/replay/legacy
        unissued observations. Default monitoring retains everything for audit.
        Distinct settled dates count anchor market sessions, so repeated weekend
        or intraday emissions cannot create additional paper trading days.
        """
        forecasts = self.forecasts()
        if run_id is not None:
            forecasts = [f for f in forecasts if f["model_version"].startswith(run_id + "-")]
        if pipeline_version is not None:
            forecasts = [f for f in forecasts if f.get("pipeline_version") == pipeline_version]
        if eligible_only or run_id is not None or pipeline_version is not None:
            forecasts = [f for f in forecasts if _eligible_for_paper_record(f)]
        matured = [f for f in forecasts if "actual_return" in f]
        by_horizon = {}
        for h in (1, 5, 20):
            rows = [f for f in matured if f["horizon"] == h]
            if rows:
                bins = []
                for low in np.arange(0, 1, 0.2):
                    b = [r for r in rows if low <= r["probability_up"] <= low + .2
                         and (low == 0 or r["probability_up"] > low)]
                    if b:
                        bins.append(dict(low=float(low), count=len(b),
                                         predicted=float(np.mean([r["probability_up"] for r in b])),
                                         observed=float(np.mean([r["actual_return"] > 0 for r in b]))))
                by_horizon[str(h)] = dict(count=len(rows), brier=float(np.mean([r["brier"] for r in rows])),
                    mae=float(np.mean([r["absolute_error"] for r in rows])),
                    coverage=float(np.mean([r["covered"] for r in rows])), calibration=bins)
        return dict(total=len(forecasts), settled=len(matured), pending=len(forecasts)-len(matured),
                    settled_decision_dates=len({r["anchor_date"] for r in matured if r["horizon"] == 1}),
                    by_horizon=by_horizon,
                    correction_policy="Refit and calibrate a versioned challenger using matured labels; predictions are immutable.")
