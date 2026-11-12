"""Offline explained screening, adapted from trader-skill's screen.py.

Copyright (c) 2026 Jake Schincariol. MIT; see docs/licenses/TRADER_SKILLS_MIT.txt.
Upstream commit: 5189e79b9a480755a3733ac9713f5353981dbe18.
Units are those of the input (no implicit percent/currency conversion).
"""
from __future__ import annotations

import json
import math
from numbers import Real
import operator
from pathlib import Path

import numpy as np
import pandas as pd


COMPARISONS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt,
               '<=': operator.le, '==': operator.eq, '!=': operator.ne}


def finite_number(value):
    return isinstance(value, Real) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {'name', 'rules', 'rank', 'missing'}:
        raise ValueError('Unknown screen configuration fields')
    if spec.get('missing', 'fail') not in ('fail', 'skip'):
        raise ValueError('missing must be fail or skip')
    for key in ('rules', 'rank'):
        if not isinstance(spec.get(key, []), list):
            raise ValueError(f'{key} must be a list')
    for rule in spec.get('rules', []):
        if (not isinstance(rule, dict) or set(rule) != {'metric', 'op', 'value'}
                or not isinstance(rule['metric'], str) or not rule['metric']):
            raise ValueError('Each rule needs metric, op and value')
        op, value = rule['op'], rule['value']
        if op not in (*COMPARISONS, 'between', 'in', 'not in'):
            raise ValueError('Unknown comparison operator')
        if op == 'between':
            if not isinstance(value, list) or len(value) != 2 or not all(map(finite_number, value)) or value[0] > value[1]:
                raise ValueError('between needs finite [low, high] in increasing order')
        elif op in {'in', 'not in'}:
            if not isinstance(value, list) or not value or not all(isinstance(v, str) or finite_number(v) for v in value):
                raise ValueError('Membership needs a nonempty finite scalar list')
        elif not finite_number(value) and not (op in {'==', '!='} and isinstance(value, str)):
            raise ValueError('Numeric comparisons need finite numeric thresholds')
    seen = set()
    for key in spec.get('rank', []):
        if (not isinstance(key, dict) or set(key) - {'metric', 'order', 'weight'}
                or not isinstance(key.get('metric'), str) or not key['metric']
                or key.get('order', 'desc') not in ('asc', 'desc')
                or not finite_number(key.get('weight', 1)) or key.get('weight', 1) <= 0
                or key['metric'] in seen):
            raise ValueError('Ranking needs distinct metrics, asc/desc and positive finite weights')
        seen.add(key['metric'])


def _check(row, rule):
    metric, op, threshold = rule['metric'], rule['op'], rule['value']
    value = row.get(metric)
    missing = value is None or value is pd.NA or isinstance(value, Real) and math.isnan(value)
    if missing:
        status = 'MISSING'
    else:
        categorical = isinstance(value, str) and (op in {'==', '!='} and isinstance(threshold, str)
                                                 or op in {'in', 'not in'})
        if not finite_number(value) and not categorical:
            status = 'INVALID'
        elif op in {'in', 'not in'}:
            inside = value in threshold
            status = 'PASS' if inside == (op == 'in') else 'FAIL'
        elif op == 'between':
            status = 'PASS' if threshold[0] <= value <= threshold[1] else 'FAIL'
        elif isinstance(threshold, str) != isinstance(value, str):
            status = 'INVALID'
        else:
            status = 'PASS' if COMPARISONS[op](value, threshold) else 'FAIL'
    return dict(metric=metric, op=op, value=value if finite_number(value) or isinstance(value, str) else None,
                threshold=threshold, status=status)


def screen(frame: pd.DataFrame, spec: dict) -> list[dict]:
    """Evaluate one cross-section. Missing/skipped rules and rank gaps stay visible.

    Rank is the weighted midrank percentile among passing rows, not a fitted
    probability. Missing rank values do not contribute; a singleton scores .5.
    Call separately per decision date and training/evaluation universe.
    """
    validate_spec(spec)
    if frame.empty:
        return []
    if 'ticker' not in frame or not frame.ticker.map(lambda v: isinstance(v, str) and bool(v.strip())).all():
        raise ValueError('Metrics need nonempty string tickers (preserve leading zeros)')
    if frame.ticker.duplicated().any():
        raise ValueError('Metrics contain duplicate tickers; screen each date separately')
    results = []
    for row in frame.to_dict('records'):
        checks = [_check(row, r) for r in spec.get('rules', [])]
        failed = any(c['status'] in {'FAIL', 'INVALID'} or c['status'] == 'MISSING'
                     and spec.get('missing', 'fail') == 'fail' for c in checks)
        results.append(dict(ticker=row['ticker'], passed=not failed, checks=checks,
                            score=None, rank_parts={}, rank_gaps=[]))
    winners = {r['ticker']: r for r in results if r['passed']}
    rows = frame.set_index('ticker')
    keys = spec.get('rank', [])
    for key in keys:
        metric = key['metric']
        values = {t: rows.loc[t, metric] for t in winners if metric in rows and finite_number(rows.loc[t, metric])}
        if values:
            v = pd.Series(values)
            ranks = (v.rank(method='average') - 1) / (len(v) - 1) if len(v) > 1 else v * 0 + .5
            if key.get('order', 'desc') == 'asc':
                ranks = 1 - ranks
            for ticker, rank in ranks.items():
                winners[ticker]['rank_parts'][metric] = float(rank)
    # Scale weights before summing to avoid overflow for valid extreme inputs.
    raw_weights = {k['metric']: k.get('weight', 1) for k in keys}
    for result in winners.values():
        parts = result['rank_parts']
        scale = max((raw_weights[m] for m in parts), default=1)
        weights = {m: raw_weights[m] / scale for m in parts}
        denominator = sum(weights[m] for m in parts)
        if denominator:
            result['score'] = sum(parts[m] * weights[m] for m in parts) / denominator
        result['rank_gaps'] = [k['metric'] for k in keys if k['metric'] not in parts]
    return sorted(results, key=lambda r: (not r['passed'], -(r['score'] if r['score'] is not None else -1), r['ticker']))


def load_metrics(path):
    """Read normalized numeric JSON/CSV without guessing units or stock codes."""
    path = Path(path)
    if path.suffix.lower() == '.csv':
        return pd.read_csv(path, dtype={'ticker': str})
    value = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(value, dict):
        value = value.get('rows')
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError('Metrics JSON needs a list of rows or {rows: [...]}')
    return pd.DataFrame(value)
