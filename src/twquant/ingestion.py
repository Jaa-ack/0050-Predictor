"""Explicit CSV and FinMind adapters; data errors never fall back to fake prices."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

import pandas as pd
import requests

from .data import redact_metadata, validate_prices
from .credentials import redact

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


class IngestionError(RuntimeError):
    """A provider request or provider payload failed validation."""


def _sanitize_strings(value, secret: str | None):
    return redact(value,[secret] if secret else [])


def _archive_json(payload: dict, params: dict, raw_dir: Path | None, token: str | None):
    if raw_dir is None:
        return
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    document = {
        "retrieved_at": stamp.isoformat(), "provider": "finmind", "endpoint": FINMIND_URL,
        "params": params, "response": payload,
    }
    body = json.dumps(_sanitize_strings(document, token), ensure_ascii=False, indent=2).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    name = f"{stamp.strftime('%Y%m%dT%H%M%S%fZ')}_{params['dataset']}_{uuid.uuid4().hex[:8]}"
    with (raw_dir / f"{name}.json").open("xb") as handle:
        handle.write(body)
    with (raw_dir / f"{name}.sha256").open("x", encoding="utf-8") as handle:
        handle.write(digest + "\n")


def _get_dataset(dataset: str, ticker: str, start: str, end: str, token: str | None,
                 raw_dir: Path | None) -> pd.DataFrame:
    params = {"dataset": dataset, "data_id": ticker, "start_date": start, "end_date": end}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = None
    for attempt in range(3):
        try:
            response = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise IngestionError(f"FinMind network request failed for {dataset}/{ticker}") from None
            time.sleep(0.5 * (2 ** attempt))
            continue
        if response.status_code >= 500 and attempt < 2:
            time.sleep(0.5 * (2 ** attempt))
            continue
        break
    if response is None:
        raise IngestionError(f"FinMind returned no response for {dataset}/{ticker}")
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise IngestionError(f"FinMind returned invalid JSON for {dataset}/{ticker} (HTTP {response.status_code})") from exc
    if not isinstance(payload, dict):
        raise IngestionError(f"FinMind returned an invalid payload for {dataset}/{ticker}")
    _archive_json(payload, params, raw_dir, token)
    if response.status_code != 200 or payload.get("status") != 200:
        message = _sanitize_strings(str(payload.get("msg", "provider rejected request")), token)
        raise IngestionError(
            f"FinMind {dataset}/{ticker}: HTTP {response.status_code}, status {payload.get('status')}: {message}"
        )
    if not isinstance(payload.get("data"), list) or not payload["data"]:
        raise IngestionError(f"FinMind returned no data for {dataset}/{ticker} in requested dates")
    return pd.DataFrame(payload["data"])


def ingest_finmind(tickers: list[str], start: str, end: str, token: str | None = None,
                   raw_dir: Path | None = None) -> pd.DataFrame:
    """Get raw OHLCV and independently adjusted closes, with strict date alignment.

    FinMind does not provide historical receipt timestamps here. 20:00 Taipei is
    a labelled reconstruction assumption, not proof of point-in-time availability.
    An inaccessible adjusted endpoint aborts the import rather than using raw data.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        raise ValueError("start must be on or before end")
    if not tickers or any(not isinstance(ticker, str) or not ticker.strip() for ticker in tickers):
        raise ValueError("tickers must be nonempty string identifiers")
    frames = []
    for ticker in dict.fromkeys(ticker.strip() for ticker in tickers):
        raw = _get_dataset("TaiwanStockPrice", ticker, start, end, token, raw_dir)
        adjusted = _get_dataset("TaiwanStockPriceAdj", ticker, start, end, token, raw_dir)
        raw_required = {"date", "stock_id", "open", "max", "min", "close", "Trading_Volume"}
        adj_required = {"date", "stock_id", "close"}
        if not raw_required.issubset(raw) or not adj_required.issubset(adjusted):
            raise IngestionError(f"FinMind price schema mismatch for {ticker}")
        for frame in (raw, adjusted):
            frame["stock_id"] = frame.stock_id.astype(str)
            frame["date"] = pd.to_datetime(frame.date, errors="raise")
            if not frame.stock_id.eq(ticker).all():
                raise IngestionError(f"FinMind returned unexpected ticker for {ticker}")
            if frame.duplicated(["date", "stock_id"]).any():
                raise ValueError(f"duplicate FinMind date rows for {ticker}")
            if frame.date.lt(start_ts).any() or frame.date.gt(end_ts).any():
                raise IngestionError(f"FinMind returned dates outside requested range for {ticker}")
        keys = ["date", "stock_id"]
        if set(map(tuple, raw[keys].to_numpy())) != set(map(tuple, adjusted[keys].to_numpy())):
            raise ValueError(f"Raw/adjusted price date alignment is incomplete for {ticker}")
        prices = raw.merge(adjusted[keys + ["close"]].rename(columns={"close": "adj_close"}),
                           on=keys, validate="one_to_one")
        prices = prices.rename(columns={"stock_id": "ticker", "max": "high", "min": "low",
                                        "Trading_Volume": "volume"})
        prices["available_at"] = (prices.date.dt.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).dt.tz_convert("UTC")
        prices["source"] = "finmind"
        prices["adjustment_source"] = "finmind_TaiwanStockPriceAdj"
        prices["availability_basis"] = "reconstructed_eod"
        prices["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        keep = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume",
                "available_at", "source", "adjustment_source", "availability_basis", "retrieved_at"]
        frames.append(prices[keep])
    return validate_prices(pd.concat(frames, ignore_index=True))


def ingest_csv(path: Path | str) -> pd.DataFrame:
    """Import an explicit adjusted-price CSV without changing the source file.

    Raw source preservation is handled by the caller's chosen archive directory;
    this adapter is read-only and never writes next to a user's source CSV.
    """
    return validate_prices(pd.read_csv(Path(path), dtype={"ticker": str}))
