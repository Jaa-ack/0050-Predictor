"""Point-in-time features and explicit future targets for Taiwan equity research.

Only columns starting with ``f_`` may enter a model. Future execution prices and
labels remain in the research frame for evaluation, never in that feature list.
Observed market sessions define horizons; individual or ETF suspensions do not shorten them.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
from .universe import TRAINING_TICKERS,BENCHMARK,asset_class

HORIZONS = (1, 5, 20)
FEATURE_VERSION = "pit-eod-v4-fixed-ten-context"


def observed_market_sessions(prices):
    """Observed daily bars establish open sessions, including a suspended ETF's gaps.

    This is not a complete exchange calendar when every supplied feed is missing.
    Future/preopen issuance still needs the separately verified exchange calendar.
    """
    if not prices.ticker.astype(str).eq('0050').any():
        raise ValueError('0050 benchmark prices are required')
    reference=prices.loc[prices.ticker.astype(str).isin((*TRAINING_TICKERS,BENCHMARK))]
    return pd.DatetimeIndex(pd.to_datetime(reference.date).dt.normalize().unique(),name='date').sort_values()


def _timestamps(values, name, allow_missing=False):
    if any(pd.Timestamp(value).tzinfo is None for value in values if not pd.isna(value)):
        raise ValueError(f"{name} must contain explicit timezone offsets")
    # Archives and Parquet may carry different timestamp resolutions.
    result = pd.to_datetime(values, utc=True).astype('datetime64[ns, UTC]')
    if not allow_missing and result.isna().any():
        raise ValueError(f"{name} cannot contain missing timestamps")
    return result


def get_feature_columns(frame: pd.DataFrame) -> list[str]:
    """Return the explicit allowlist of model inputs, excluding all labels."""
    return [column for column in frame.columns if column.startswith("f_")]


def select_feature_columns(frame,profile='legacy'):
    columns=get_feature_columns(frame)
    if profile=='legacy':return columns
    if profile not in {'portable_price','portable_market','portable_all','portable_path','portable_sector'}:
        raise ValueError('Unknown feature profile')
    result=[]
    for column in columns:
        if column.startswith('f_path_') and profile not in {'portable_path','portable_sector'}:continue
        if column.startswith('f_ext_us_sector_') and profile != 'portable_sector':continue
        if column in {'f_log_volume','f_amihud_20'}:continue
        if column.startswith('f_ext_'):
            if profile=='portable_price':continue
            if '_close' in column:continue
            if profile in {'portable_market','portable_path'} and not column.startswith(('f_ext_fmp_', 'f_ext_taifex_')):continue
            if profile=='portable_sector' and not column.startswith(('f_ext_fmp_','f_ext_taifex_','f_ext_us_sector_')):continue
        result.append(column)
    return result


def point_in_time_join(decisions: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Join the latest *available* numeric event, preserving prior revisions.

    Event values are long-form: ticker, feature, value, published_at,
    available_at. A ``*`` ticker represents a global macro feature. A revision
    becomes visible only from its own available_at; no backfill is performed.
    Optional expires_at is inclusive. An expired latest event stays unknown;
    an older value is not revived. Missing expiry means no declared expiry.
    """
    result = decisions.copy()
    if events is None or events.empty:
        return result
    required = {"ticker", "feature", "value", "published_at", "available_at"}
    missing = required.difference(events.columns)
    if missing:
        raise ValueError(f"Events missing columns: {sorted(missing)}")
    if not {"ticker", "decision_ts"}.issubset(result.columns):
        raise ValueError("Decisions require ticker and decision_ts")
    data = events.copy()
    data["ticker"] = data.ticker.astype(str)
    data["published_at"] = _timestamps(data.published_at, "published_at")
    data["available_at"] = _timestamps(data.available_at, "available_at")
    data['expires_at'] = (_timestamps(data.expires_at, 'expires_at', allow_missing=True)
                          if 'expires_at' in data else pd.Series(pd.NaT, index=data.index, dtype='datetime64[ns, UTC]'))
    result["decision_ts"] = _timestamps(result.decision_ts, "decision_ts", allow_missing=True)
    if (data.published_at > data.available_at).any():
        raise ValueError("published_at must be <= available_at")
    if (data.expires_at.notna() & data.expires_at.lt(data.available_at)).any():
        raise ValueError('expires_at must not precede available_at')
    if not data.feature.map(lambda value: isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value))).all():
        raise ValueError("feature names must be short alphanumeric identifiers")
    data["value"] = pd.to_numeric(data.value, errors="raise")
    if not np.isfinite(data.value.to_numpy(dtype=float)).all():
        raise ValueError("Event values must be finite")
    data["version"] = data.get("version", pd.Series(0, index=data.index)).fillna(0)
    # Stable version sorting makes same-time corrections deterministic.
    data = data.sort_values(["available_at", "version"], kind="stable")
    positional = result.reset_index(drop=True)
    for feature in sorted(data.feature.unique()):
        col = f"f_ext_{feature}"
        ages = f"{col}_age_days"
        positional[col] = np.nan
        positional[ages] = np.nan
        for ticker, group in positional.groupby("ticker", sort=False):
            candidates = data.loc[(data.feature == feature) & data.ticker.isin([str(ticker), "*"])].copy()
            if candidates.empty:
                continue
            # A security-specific record wins a global record at the same time.
            candidates["specific"] = (candidates.ticker == str(ticker)).astype(int)
            candidates = candidates.sort_values(["available_at", "specific", "version"], kind="stable")
            group = group.loc[group.decision_ts.notna()]
            if group.empty:
                continue
            left = group[["decision_ts"]].copy()
            left["position"] = group.index
            merged = pd.merge_asof(left.sort_values("decision_ts"),
                                   candidates[["available_at", "value", "expires_at"]],
                                   left_on="decision_ts", right_on="available_at", direction="backward")
            positions = merged.position.to_numpy()
            active = merged.expires_at.isna() | merged.decision_ts.le(merged.expires_at)
            positional.loc[positions, col] = merged.value.where(active).to_numpy()
            positional.loc[positions, ages] = ((merged.decision_ts - merged.available_at).dt.total_seconds() / 86_400).where(active).to_numpy()
    positional.index = result.index
    return positional


def build_features(prices: pd.DataFrame, external: pd.DataFrame | None = None,
                   mode: str = "postclose") -> pd.DataFrame:
    """Build past-only EOD factors and 1/5/20 observed market-session labels.

    Postclose decisions use 20:00 Asia/Taipei. Preopen decisions use 08:30 of
    the next observed market session and anchor on the preceding close. The
    terminal preopen decision is NaT until a verified next session is available;
    it is retained for a caller to set an explicit, verified inference cutoff.
    Late price rows are conservatively omitted, including from rolling history.
    Historical availability reconstruction is not an observed PIT archive.
    """
    if mode not in {"postclose", "preopen"}:
        raise ValueError("mode must be postclose or preopen")
    required = {"ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "available_at"}
    missing = required.difference(prices.columns)
    if missing:
        raise ValueError(f"Prices missing columns: {sorted(missing)}")
    data = prices.copy()
    data["ticker"] = data.ticker.astype(str)
    data["date"] = pd.to_datetime(data.date).dt.tz_localize(None).dt.normalize()
    data["available_at"] = _timestamps(data.available_at, "available_at")
    if data.duplicated(["ticker", "date"]).any():
        raise ValueError("Duplicate ticker/date prices")
    numeric = ["open", "high", "low", "close", "adj_close", "volume"]
    data[numeric] = data[numeric].apply(pd.to_numeric, errors="raise")
    finite_values = np.isfinite(data[numeric].to_numpy(dtype=float))
    # A price index has no executable volume requirement. Preserve vendor
    # missing volume; do not fabricate zero activity or relax equity checks.
    index_rows = data.ticker.map(asset_class).eq("index")
    finite_values[:, -1] |= (index_rows & data.volume.isna()).to_numpy()
    if not finite_values.all():
        raise ValueError("Prices and volumes must be finite")
    if (data[["open", "high", "low", "close", "adj_close"]] <= 0).any().any() or (data.volume < 0).any():
        raise ValueError("Prices must be positive and volume nonnegative")
    benchmark = data.loc[data.ticker == "0050"].set_index("date").sort_index()
    if benchmark.empty:
        raise ValueError("0050 benchmark prices are required for the session calendar")
    calendar = observed_market_sessions(data)
    eod_times = (calendar.tz_localize("Asia/Taipei") + pd.Timedelta(hours=20)).tz_convert("UTC")
    if mode == "postclose":
        decision_times = eod_times
    else:
        next_sessions = pd.DatetimeIndex(pd.Series(calendar).shift(-1))
        decision_times = (next_sessions.tz_localize("Asia/Taipei") + pd.Timedelta(hours=8, minutes=30)).tz_convert("UTC")
    # Without a verified terminal next session, only the preceding EOD data may
    # form terminal technical factors. External inputs remain missing at NaT.
    visibility_times = decision_times.where(decision_times.notna(), eod_times)
    benchmark = benchmark.reindex(calendar)
    # All predictive market context comes from the fixed ten. 0050 remains
    # a reference calendar and outcome benchmark, never a training factor.
    core = data.loc[data.ticker.isin(TRAINING_TICKERS)]
    closes = core.pivot(index="date", columns="ticker", values="adj_close").reindex(calendar)
    receipts = core.pivot(index="date", columns="ticker", values="available_at").reindex(calendar)
    visible_core = receipts.le(pd.Series(visibility_times, index=calendar), axis=0)
    core_known = closes.where(visible_core)
    core_return = core_known.pct_change(fill_method=None).mean(axis=1)
    core_market_returns = {h: np.expm1(np.log1p(core_return).rolling(h, min_periods=h).sum())
                           for h in (5, 20, 60)}
    frames = []
    for ticker, source in data.groupby("ticker", sort=True):
        truth = source.set_index("date").sort_index().reindex(calendar)
        visible = truth.available_at.notna() & (truth.available_at.to_numpy() <= visibility_times.to_numpy())
        known = truth.where(visible, axis=0)
        adj = known.adj_close
        factor = known.adj_close / known.close
        adj_open = known.open * factor
        adj_high = known.high * factor
        adj_low = known.low * factor
        ret = adj.pct_change(fill_method=None)
        out = truth.copy()
        out["ticker"] = ticker
        out['asset_class']=asset_class(ticker)
        out["decision_ts"] = decision_times
        out["feature_version"] = FEATURE_VERSION
        for horizon in (1, 5, 10, 20, 60, 120, 250):
            out[f"f_return_{horizon}"] = adj.pct_change(horizon, fill_method=None)
        out["f_volatility_20"] = ret.rolling(20, min_periods=15).std()
        out["f_volatility_60"] = ret.rolling(60, min_periods=40).std()
        out["f_gap"] = adj_open / adj.shift(1) - 1
        out["f_intraday"] = adj / adj_open - 1
        out["f_range"] = (adj_high - adj_low) / adj
        out["f_close_location"] = ((adj - adj_low) / (adj_high - adj_low).replace(0, np.nan))
        out["f_rvol_20"] = known.volume / known.volume.shift(1).rolling(20, min_periods=15).mean().replace(0, np.nan)
        out["f_log_volume"] = np.log1p(known.volume)
        out["f_amihud_20"] = (ret.abs() / (known.close * known.volume).replace(0, np.nan)).rolling(20, min_periods=15).mean() * 1e9
        out["f_price_ma20"] = adj / adj.rolling(20, min_periods=15).mean() - 1
        delta = adj.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        out["f_rsi_14"] = (gain / (gain + loss)).where((gain + loss) != 0, 0.5)
        out["f_market_return_1"] = core_return
        for horizon in (5, 20, 60):
            out[f"f_market_return_{horizon}"] = core_market_returns[horizon]
            out[f"f_excess_return_{horizon}"] = out[f"f_return_{horizon}"] - out[f"f_market_return_{horizon}"]
        out["f_beta_60"] = ret.rolling(60, min_periods=30).cov(core_return) / core_return.rolling(60, min_periods=30).var().replace(0, np.nan)
        # Future values below are evaluation metadata; get_feature_columns excludes them.
        out["next_open"] = truth.open.shift(-1)
        out["next_open_date"] = pd.Series(calendar, index=calendar).shift(-1)
        out["next_open_adjustment"] = (truth.adj_close / truth.close).shift(-1)
        out["adjusted_next_open"] = out.next_open * out.next_open_adjustment
        out["next_high"] = truth.high.shift(-1)
        out["next_low"] = truth.low.shift(-1)
        out["next_volume"] = truth.volume.shift(-1)
        bm_next_open = (benchmark.open * benchmark.adj_close / benchmark.close).shift(-1)
        out["benchmark_adj_close"] = benchmark.adj_close
        out["benchmark_adjusted_next_open"] = bm_next_open
        # Daily range is a future outcome, so it must not filter opening orders.
        # Queue/limit fillability requires timestamped order-book data; this
        # daily-bar simulator assumes an opening fill when actual trading exists.
        executable_open = out.next_open.gt(0) & (out.next_volume > 0)
        for horizon in HORIZONS:
            end_close = truth.adj_close.shift(-horizon)
            out[f"target_{horizon}"] = np.log(end_close / truth.adj_close)
            out[f"execution_target_{horizon}"] = np.log(end_close / out.adjusted_next_open)
            out[f"label_end_{horizon}"] = pd.Series(calendar, index=calendar).shift(-horizon)
            out[f"label_available_at_{horizon}"] = truth.available_at.shift(-horizon)
            out[f"benchmark_target_{horizon}"] = np.log(benchmark.adj_close.shift(-horizon) / benchmark.adj_close)
            out[f"benchmark_end_adj_close_{horizon}"] = benchmark.adj_close.shift(-horizon)
            valid = executable_open & end_close.notna()
            out[f"trade_valid_{horizon}"] = valid & (asset_class(ticker)!='index')
            out[f"trade_return_{horizon}"] = (end_close / out.adjusted_next_open - 1).where(valid)
            out[f"benchmark_trade_return_{horizon}"] = benchmark.adj_close.shift(-horizon) / bm_next_open - 1
        out = out.loc[visible].reset_index()
        frames.append(out)
    frame = pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"]).reset_index(drop=True)
    # Equal-weight peers exclude the target. This is deliberately named as a
    # proxy, not as a historical weight-adjusted leave-one-out 0050 return.
    stocks = frame.loc[frame.ticker.isin(TRAINING_TICKERS), ["date", "f_return_1"]]
    peer = stocks.groupby("date").f_return_1.agg(["sum", "count"])
    frame["_peer_sum"] = frame.date.map(peer["sum"])
    frame["_peer_count"] = frame.date.map(peer["count"])
    include = frame.ticker.isin(TRAINING_TICKERS) & frame.f_return_1.notna()
    denominator = frame._peer_count - include.astype(int)
    frame["f_peer_return_ex_target"] = ((frame._peer_sum - frame.f_return_1.where(include, 0)) / denominator.replace(0, np.nan))
    frame = frame.drop(columns=["_peer_sum", "_peer_count"])
    frame[get_feature_columns(frame)] = frame[get_feature_columns(frame)].replace([np.inf, -np.inf], np.nan)
    return point_in_time_join(frame, external) if external is not None else frame
