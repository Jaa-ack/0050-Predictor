"""Validated price contracts and immutable, checksum-addressed research snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

import duckdb
import numpy as np
import pandas as pd

REQUIRED_PRICE_COLUMNS = (
    "ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "available_at",
)
_SENSITIVE_KEYS = {"token", "api_key", "apikey", "secret", "password", "authorization", "access_token"}
_SNAPSHOT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def redact_metadata(value):
    """Remove credentials recursively before archival; request headers are never saved."""
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact_metadata(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_metadata(item) for item in value]
    return value


def validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted copy with daily dates/UTC timestamps, or reject invalid data.

    ``adj_close`` is mandatory: silently using unadjusted prices would fabricate
    returns around distributions and splits. Availability timestamps require an
    explicit offset; historical assumptions must be labelled by the importer.
    """
    missing = set(REQUIRED_PRICE_COLUMNS) - set(prices.columns)
    if missing:
        raise ValueError(f"Missing required price columns: {', '.join(sorted(missing))}")
    if prices.empty:
        raise ValueError("Price data must contain at least one row")
    out = prices.copy()
    if out.ticker.isna().any():
        raise ValueError("ticker contains missing values")
    out["ticker"] = out.ticker.astype(str).str.strip()
    if out.ticker.eq("").any() or out.ticker.str.contains(r"[\s/\\]").any():
        raise ValueError("ticker contains an invalid identifier")
    try:
        dates = pd.to_datetime(out.date, errors="raise")
    except (ValueError, TypeError) as exc:
        raise ValueError("date must contain valid daily dates") from exc
    if dates.isna().any() or dates.dt.tz is not None or not dates.eq(dates.dt.normalize()).all():
        raise ValueError("date must be timezone-naive midnight session dates")
    out["date"] = dates
    if out.duplicated(["ticker", "date"]).any():
        raise ValueError("duplicate ticker/date price rows")
    for column in ("open", "high", "low", "close", "adj_close", "volume"):
        values = pd.to_numeric(out[column], errors="coerce")
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            raise ValueError(f"{column} contains missing or non-finite values")
        if (values.lt(0) if column == "volume" else values.le(0)).any():
            raise ValueError(f"{column} must be {'nonnegative' if column == 'volume' else 'positive'}")
        out[column] = values.astype(float)
    tolerance = 1e-8
    invalid_range = (
        out.high.lt(out[["open", "close", "low"]].max(axis=1) - tolerance)
        | out.low.gt(out[["open", "close", "high"]].min(axis=1) + tolerance)
    )
    if invalid_range.any():
        raise ValueError("OHLC price range is inconsistent")
    try:
        aware = out.available_at.map(lambda value: pd.Timestamp(value))
        if any(pd.isna(value) or value.tzinfo is None for value in aware):
            raise ValueError("available_at requires an explicit timezone offset")
        out["available_at"] = pd.to_datetime(out.available_at, utc=True, errors="raise")
    except (ValueError, TypeError) as exc:
        raise ValueError("available_at requires valid timestamps with an explicit timezone offset") from exc
    # A complete regular-session daily bar cannot precede the physical close.
    # This is only a minimum floor; actual vendor/after-hours completeness may
    # require a later receipt time (the FinMind reconstruction uses 20:00).
    earliest = (out.date.dt.tz_localize("Asia/Taipei")
                + pd.Timedelta(hours=13, minutes=30)).dt.tz_convert("UTC")
    if out.available_at.lt(earliest).any():
        raise ValueError("Daily OHLC available_at precedes the 13:30 Asia/Taipei closing floor")
    if "source" not in out:
        out["source"] = "user_csv"
    if "adjustment_source" not in out:
        out["adjustment_source"] = "user_supplied_unverified"
    if "availability_basis" not in out:
        out["availability_basis"] = "user_supplied"
    for column in ("source", "adjustment_source", "availability_basis"):
        if out[column].isna().any() or out[column].astype(str).str.strip().eq("").any():
            raise ValueError(f"{column} must not be empty")
        out[column] = out[column].astype(str)
    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class PriceStore:
    """One directory per immutable normalized Parquet snapshot, queried by DuckDB."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_PATTERN.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot identifier")
        return self.snapshots_dir / snapshot_id

    def write_prices(self, prices: pd.DataFrame, metadata: dict | None = None) -> str:
        normalized = validate_prices(prices)
        created_at = datetime.now(timezone.utc)
        snapshot_id = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:10]}"
        destination = self._snapshot_dir(snapshot_id)
        destination.mkdir(parents=True, exist_ok=False)
        parquet_path = destination / "prices.parquet"
        normalized.to_parquet(parquet_path, index=False)
        manifest = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "created_at": created_at.isoformat(),
            "sha256": _sha256(parquet_path),
            "rows": len(normalized),
            "tickers": sorted(normalized.ticker.unique().tolist()),
            "min_date": normalized.date.min().date().isoformat(),
            "max_date": normalized.date.max().date().isoformat(),
            "sources": sorted(normalized.source.unique().tolist()),
            "availability_bases": sorted(normalized.availability_basis.unique().tolist()),
            "metadata": redact_metadata(metadata or {}),
        }
        with (destination / "manifest.json").open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, default=str)
        return snapshot_id

    def list_snapshots(self) -> list[dict]:
        if not self.snapshots_dir.exists():
            return []
        manifests = []
        for path in sorted(self.snapshots_dir.glob("*/manifest.json")):
            if (path.parent / "prices.parquet").is_file():
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(manifests, key=lambda item: item["created_at"])

    def latest_snapshot_id(self) -> str:
        manifests = self.list_snapshots()
        if not manifests:
            raise FileNotFoundError(f"No price snapshots found under {self.root}")
        return manifests[-1]["snapshot_id"]

    def read_prices(self, snapshot_id: str | None = None) -> pd.DataFrame:
        selected_id = snapshot_id or self.latest_snapshot_id()
        directory = self._snapshot_dir(selected_id)
        manifest_path = directory / "manifest.json"
        parquet_path = directory / "prices.parquet"
        if not manifest_path.is_file() or not parquet_path.is_file():
            raise FileNotFoundError(f"Price snapshot not found: {selected_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if _sha256(parquet_path) != manifest["sha256"]:
            raise ValueError(f"Price snapshot checksum mismatch: {selected_id}")
        with duckdb.connect(":memory:") as connection:
            prices = connection.execute(
                "SELECT * FROM read_parquet(?) ORDER BY ticker, date", [str(parquet_path)]
            ).arrow().read_all().to_pandas()
        result = validate_prices(prices)
        result.attrs["snapshot_id"] = selected_id
        result.attrs["manifest"] = manifest
        return result
