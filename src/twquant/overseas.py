"""Incremental overseas information between a completed Taiwan anchor and decision.

This is a research feature transformation, not an exchange calendar or proof
that reconstructed provider availability represents an original PIT archive.
"""
from __future__ import annotations

import pandas as pd

from .features import point_in_time_join


def add_overseas_window_features(frame, events):
    """Append scale-free US close changes available after anchor-day 20:00.

    Two backward as-of joins use the same global FMP close event stream. No new
    visible receipt gives a zero move; absent starting/ending data stays NaN.
    An updated receipt with an unchanged value is distinguished from no update.
    Existing inputs, targets, row order and index are preserved. Stock histories
    never fit these features or supply global context.
    """
    if not {'ticker', 'date', 'decision_ts'}.issubset(frame.columns):
        raise ValueError('Overseas windows require ticker, date and decision_ts')
    if events is None or not {'ticker', 'feature', 'value'}.issubset(events.columns):
        raise ValueError('Overseas windows require global FMP close events')
    closes = events.loc[events.feature.astype(str).str.fullmatch(r'fmp_[A-Za-z0-9_]+_close')].copy()
    if closes.empty or not closes.ticker.astype(str).eq('*').all():
        raise ValueError('Overseas windows require global FMP close events')
    closes['value'] = pd.to_numeric(closes.value, errors='raise')
    if closes.value.le(0).any():
        raise ValueError('Overseas close values must be positive')
    decisions = frame[['ticker', 'decision_ts']].copy()
    current = point_in_time_join(decisions, closes)
    anchor_date = pd.to_datetime(frame.date).dt.tz_localize(None).dt.normalize()
    if anchor_date.isna().any():
        raise ValueError('Overseas windows require daily anchor dates')
    anchor = (anchor_date.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
    decision = pd.to_datetime(current.decision_ts, utc=True)
    if (decision.notna() & decision.lt(anchor)).any():
        raise ValueError('Overseas window decisions cannot precede anchor-day 20:00')
    reference = point_in_time_join(decisions.assign(decision_ts=anchor), closes)
    elapsed_days = (decision - anchor).dt.total_seconds() / 86_400
    result = frame.copy()
    for feature in sorted(closes.feature.unique()):
        col = f'f_ext_{feature}'
        prefix = f'f_ext_{feature.removesuffix("_close")}_window'
        known = reference[col].notna() & current[col].notna() & decision.notna()
        result[f'{prefix}_return'] = (current[col] / reference[col] - 1).where(known)
        result[f'{prefix}_updated'] = current[f'{col}_age_days'].lt(elapsed_days).astype(float).where(known)
    return result
