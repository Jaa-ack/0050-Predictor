"""Counterfactual opening-reference inputs; always synthetic availability evidence.

The next raw open is an evaluation field, deliberately used only by this
isolated simulation. It is never asserted to be an observed preopen quote.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from scipy.special import ndtri

from .features import _timestamps


def add_opening_reference_proxy(frame, *, noise_bps=0., missing_fraction=0., seed=17):
    """Assume a noisy next raw opening reference at the existing08:30 cutoff.

    Hash-keyed independent normal errors and missingness do not depend on row
    order, other assets or future answers. Raw prices avoid reading a future
    adjustment factor. Missing references retain rows and baseline features.
    """
    if (not np.isfinite(noise_bps) or noise_bps < 0 or not np.isfinite(missing_fraction)
            or not 0 <= missing_fraction <= 1 or isinstance(seed, bool)
            or not isinstance(seed, int) or seed < 0):
        raise ValueError('Invalid opening proxy settings')
    required = {'ticker', 'date', 'decision_ts', 'close', 'next_open', 'next_open_date', 'f_volatility_20'}
    if not required.issubset(frame):
        raise ValueError('Opening proxy requires the preopen price research frame')
    if frame.duplicated(['ticker', 'date']).any():
        raise ValueError('Duplicate opening proxy ticker/date')
    date = pd.to_datetime(frame.date)
    next_date = pd.to_datetime(frame.next_open_date)
    if (date.dt.tz is not None or next_date.dt.tz is not None or date.isna().any()
            or not date.eq(date.dt.normalize()).all()
            or not next_date.dropna().eq(next_date.dropna().dt.normalize()).all()):
        raise ValueError('Opening proxy dates must be naive daily sessions')
    decisions = _timestamps(frame.decision_ts, 'opening proxy decision_ts', allow_missing=True)
    local = decisions.dt.tz_convert('Asia/Taipei')
    local_date = local.dt.tz_localize(None).dt.normalize()
    valid_clock = (local.dt.hour.eq(8) & local.dt.minute.eq(30) & local.dt.second.eq(0)
                   & local.dt.microsecond.eq(0) & local.dt.nanosecond.eq(0)
                   & local_date.eq(next_date) & local_date.gt(date))
    if not valid_clock.loc[decisions.notna()].all():
        raise ValueError('Opening proxy requires the next-session08:30 decision contract')
    keys = (f'{seed}|{ticker}|{d.date().isoformat()}' for ticker, d in zip(frame.ticker, date, strict=True))
    draws = np.frombuffer(b''.join(hashlib.blake2b(k.encode(), digest_size=16).digest() for k in keys),
                          dtype='<u8').reshape(-1, 2)
    uniforms = ((draws >> 11).astype(float) + .5) / 2**53
    uniforms = uniforms.clip(np.nextafter(0., 1.), np.nextafter(1., 0.))
    opening = pd.to_numeric(frame.next_open, errors='raise')
    previous = pd.to_numeric(frame.close, errors='raise')
    observed = (decisions.notna() & np.isfinite(opening) & opening.gt(0)
                & np.isfinite(previous) & previous.gt(0) & (uniforms[:, 1] >= missing_fraction))
    gap = np.log(opening.where(observed) / previous.where(observed))
    gap = gap + ndtri(uniforms[:, 0]) * float(noise_bps) / 10000.
    volatility = pd.to_numeric(frame.f_volatility_20, errors='raise')
    volatility = volatility.where(np.isfinite(volatility) & volatility.gt(0)).clip(lower=.0001)
    out = frame.copy()
    out['f_openref_log_gap'] = gap
    out['f_openref_scaled_gap'] = gap / volatility
    out['f_openref_present'] = observed.astype(float)
    out.attrs['opening_proxy_contract'] = dict(evidence_kind='synthetic',
        source='historical_next_raw_open_as_unobserved_reference',
        assumed_receipt='08:30 Asia/Taipei on next primary session',
        noise_bps=float(noise_bps), missing_fraction=float(missing_fraction), seed=seed,
        uses_future_open=True, uses_future_closing_labels=False,
        original_pit_verified=False, provider_data_obtained=False,
        limitation='Counterfactual reference availability, not an archived indicative quote or a realistic error model. Raw gaps retain corporate-action effects.')
    return out
