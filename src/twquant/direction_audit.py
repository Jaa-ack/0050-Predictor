"""Complete-calendar direction audits for training stocks and declared constituents."""
from __future__ import annotations

from itertools import combinations
from math import comb
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .universe import TRAINING_TICKERS, asset_class


def load_membership_snapshot(path):
    """Read a full current-0050 snapshot with an explicit metadata allowlist.

    Training roles from a document are deliberately ignored. The issuer page
    normally renders only five rows before expansion, so CLI research refuses
    a snapshot of fewer than fifty stocks. Hashes provide local traceability,
    not evidence of membership at earlier prediction dates.
    """
    path = Path(path)
    body = path.read_bytes()
    document = json.loads(body)
    if not isinstance(document, dict) or not isinstance(document.get('tickers'), list):
        raise ValueError('Membership snapshot requires a stock list')
    universe = constituent_universe(document['tickers'])
    if len(universe) < 50 or document.get('count') != len(universe):
        raise ValueError('Membership snapshot is partial or its declared count is inconsistent')
    value = document.get('as_of')
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}[-/]\d{2}[-/]\d{2}', value):
        raise ValueError('Membership snapshot requires an explicit daily as_of date')
    as_of = pd.Timestamp(value).date().isoformat()
    if document.get('source_url') != 'https://www.yuantaetfs.com/product/detail/0050/ratio':
        raise ValueError('Membership snapshot requires the ordinary official 0050 holdings URL')
    retrieved = pd.Timestamp(document.get('retrieved_at'))
    if pd.isna(retrieved) or retrieved.tzinfo is None:
        raise ValueError('Membership receipt time requires an explicit timezone')
    digest = document.get('html_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        raise ValueError('Membership snapshot requires the archived HTML checksum')
    html = path.parent / 'yuanta_0050.html'
    if html.exists() and hashlib.sha256(html.read_bytes()).hexdigest() != digest.lower():
        raise ValueError('Membership archived HTML checksum mismatch')
    return dict(tickers=list(universe), constituent_count=len(universe), as_of=as_of,
        source_url=document['source_url'], retrieved_at=retrieved.tz_convert('UTC').isoformat(),
        html_sha256=digest.lower(), html_checksum_checked=html.exists(),
        snapshot_sha256=hashlib.sha256(body).hexdigest(), historical_membership_verified=False,
        limitation='Declared current holdings only; content hashes do not establish historical membership.')


def audit_five_stock_accuracy(predictions, *, evidence_kind='unknown', expected_dates=None,
                             target_accuracy=.75, min_dates=126, bootstrap_samples=500,
                             block_dates=20, seed=42):
    """Preserve the fixed-ten audit and enumerate all 252 five-stock subsets."""
    return _audit_panel(predictions, TRAINING_TICKERS, evidence_kind=evidence_kind,
        expected_dates=expected_dates, target_accuracy=target_accuracy, min_dates=min_dates,
        bootstrap_samples=bootstrap_samples, block_dates=block_dates, seed=seed,
        scope='all_252_five_stock_subsets_of_fixed_ten_only')


def constituent_universe(values):
    """Validate an explicitly declared stock evaluation universe, never training roles."""
    if isinstance(values, str):
        raise ValueError('Provide a constituent universe as a list of distinct stock identifiers')
    universe = tuple(str(v).strip() for v in values)
    if (len(universe) < 5 or len(set(universe)) != len(universe)
        or any(not re.fullmatch(r'\d{4}', t) or asset_class(t) != 'equity' for t in universe)):
        raise ValueError('Invalid constituent universe: need at least five distinct stocks, excluding ETFs and indices')
    return universe


def audit_constituent_accuracy(predictions, constituents, *, membership_as_of,
                               evidence_kind='unknown', expected_dates=None,
                               target_accuracy=.75, min_dates=126, bootstrap_samples=500,
                               block_dates=20, seed=42):
    """Evaluate every declared constituent using the same model at each decision.

    Common denominators make the lowest-five arithmetic mean the exact minimum
    over all five-stock subsets. This covers large universes without selecting
    a lucky subset or materializing millions of records. The snapshot date does
    not establish historical membership or an untouched test.
    """
    universe = constituent_universe(constituents)
    if membership_as_of is None:
        raise ValueError('Constituent audit requires a membership snapshot date')
    date = pd.Timestamp(membership_as_of)
    if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
        raise ValueError('Membership snapshot date must be a daily date')
    if not {'model_version', 'decision_threshold'} <= set(predictions):
        raise ValueError('Constituent predictions require the same shared model and threshold at each decision')
    if predictions.model_version.isna().any() or predictions.model_version.astype(str).str.strip().eq('').any():
        raise ValueError('Constituent predictions require the same shared model and threshold at each decision')
    dates = pd.to_datetime(predictions.date).dt.normalize()
    shared = predictions.groupby(dates)[['model_version', 'decision_threshold']].nunique(dropna=False)
    if shared.gt(1).any().any():
        raise ValueError('Constituent predictions require the same shared model and threshold at each decision')
    result = _audit_panel(predictions, universe, evidence_kind=evidence_kind,
        expected_dates=expected_dates, target_accuracy=target_accuracy, min_dates=min_dates,
        bootstrap_samples=bootstrap_samples, block_dates=block_dates, seed=seed,
        scope='all_five_stock_subsets_of_declared_current_constituents')
    result.update(constituent_count=len(universe), membership_as_of=date.date().isoformat(),
                  same_shared_model_required=True)
    result['limitations'].append('The declared current snapshot does not prove membership at past prediction dates.')
    return result


def _audit_panel(predictions, universe, *, evidence_kind, expected_dates, target_accuracy,
                 min_dates, bootstrap_samples, block_dates, seed, scope):
    if evidence_kind not in {'unknown', 'synthetic', 'historical'}:
        raise ValueError('Unknown evidence kind')
    if not 0 < target_accuracy <= 1 or min_dates < 1 or bootstrap_samples < 1 or block_dates < 1:
        raise ValueError('Invalid direction audit settings')
    p = predictions.copy()
    required = {'date', 'ticker', 'horizon', 'actual_return', 'probability_up'}
    if not required <= set(p):
        raise ValueError('Direction predictions lack required columns')
    if p.horizon.nunique() != 1:
        raise ValueError('Audit requires exactly one horizon')
    p['date'] = pd.to_datetime(p.date).dt.normalize()
    p['ticker'] = p.ticker.astype(str)
    if not set(p.ticker) <= set(universe):
        raise ValueError('Direction predictions are outside the declared audit universe')
    if p.duplicated(['date', 'ticker']).any():
        raise ValueError('Direction predictions contain duplicate ticker/date rows')
    dates = pd.DatetimeIndex(p.date.unique() if expected_dates is None else expected_dates).normalize().unique().sort_values()
    if not len(dates) or dates.hasnans or not set(p.date) <= set(dates):
        raise ValueError('Invalid expected decision dates')
    grid = pd.MultiIndex.from_product([dates, universe], names=['date', 'ticker'])
    present = pd.Series(True, index=pd.MultiIndex.from_frame(p[['date', 'ticker']])).reindex(grid, fill_value=False)
    p = p.set_index(['date', 'ticker']).reindex(grid)
    actual = pd.to_numeric(p.actual_return, errors='coerce')
    probability = pd.to_numeric(p.probability_up, errors='coerce')
    threshold = pd.to_numeric(p.get('decision_threshold', pd.Series(.5, index=grid)), errors='coerce')
    valid = probability.between(0, 1) & threshold.between(0, 1)
    if 'abstain' in p:
        valid &= ~p.abstain.fillna(True).astype(bool)
    known = np.isfinite(actual)
    up = actual.gt(0)
    predicted_up = probability.ge(threshold)
    correct = (known & valid & predicted_up.eq(up)).to_numpy().reshape(len(dates), len(universe))
    stocks = {}
    for ticker in universe:
        mask = grid.get_level_values('ticker') == ticker
        k, v, y, decision = known[mask], valid[mask], up[mask], predicted_up[mask]
        successes = k & v & decision.eq(y)
        recall_up = float((v[y & k] & decision[y & k]).mean()) if (y & k).any() else None
        recall_down = float((v[~y & k] & ~decision[~y & k]).mean()) if (~y & k).any() else None
        balanced = (recall_up + recall_down) / 2 if recall_up is not None and recall_down is not None else None
        stocks[ticker] = dict(accuracy=float(successes.sum() / len(dates)),
            balanced_accuracy=balanced, observations=int(k.sum()), expected_predictions=len(dates),
            coverage=float((k & v).sum() / len(dates)), recall_up=recall_up, recall_down=recall_down,
            always_up_accuracy=float((k & y).sum() / len(dates)),
            always_down_accuracy=float((k & ~y).sum() / len(dates)))

    def subset_record(indices):
        values = correct[:, indices]
        return dict(tickers=[universe[i] for i in indices], accuracy=float(values.mean()),
                    all_five_correct_rate=float(values.all(axis=1).mean()))

    ranking = sorted(range(len(universe)), key=lambda i: (stocks[universe[i]]['accuracy'], universe[i]))
    worst = subset_record(ranking[:5])
    best = subset_record(sorted(range(len(universe)), key=lambda i: (-stocks[universe[i]]['accuracy'], universe[i]))[:5])
    combination_count = comb(len(universe), 5)
    enumerated = combination_count <= 1000
    subsets = [subset_record(indices) for indices in combinations(range(len(universe)), 5)] if enumerated else []
    subsets.sort(key=lambda s: (s['accuracy'], s['tickers']))
    minimum = min(s['accuracy'] for s in stocks.values())
    edge = all(s['balanced_accuracy'] is not None and s['balanced_accuracy'] > .5
               and s['accuracy'] > max(s['always_up_accuracy'], s['always_down_accuracy']) for s in stocks.values())
    coverage = float((known & valid).mean())
    empirical = bool(minimum >= target_accuracy and coverage == 1 and len(dates) >= min_dates and edge)
    block = min(len(dates), max(block_dates, int(predictions.horizon.iloc[0])))
    rng = np.random.default_rng(seed)
    minima = []
    for _ in range(bootstrap_samples):
        starts = rng.integers(len(dates), size=int(np.ceil(len(dates) / block)))
        sample = ((starts[:, None] + np.arange(block)) % len(dates)).ravel()[:len(dates)]
        minima.append(float(correct[sample].mean(axis=0).min()))
    return dict(evidence_kind=evidence_kind, horizon=int(predictions.horizon.iloc[0]),
        target_accuracy=target_accuracy, training_tickers=list(TRAINING_TICKERS), evaluation_tickers=list(universe),
        evaluated_stocks=len(universe), scope=scope,
        definition='Every declared stock must reach the target with full coverage. The worst five-stock mean is exact; five simultaneously correct is a separate diagnostic.',
        test_dates=len(dates), expected_predictions=len(grid), missing_predictions=int((~present).sum()),
        unresolved_labels=int((~known).sum()), coverage=coverage, overall_accuracy=float(correct.mean()),
        minimum_stock_accuracy=minimum, worst_five_accuracy=worst['accuracy'], worst_five=worst, best_five=best,
        per_stock=stocks, five_stock_combinations=subsets, combination_count=combination_count,
        all_combinations_enumerated=enumerated,
        combination_method='Exact extrema by ranking equal-denominator per-stock accuracies; enumeration only for at most 1000 subsets.',
        directional_edge_demonstrated=edge, empirical_target_met=empirical,
        simulated_target_met=empirical and evidence_kind == 'synthetic', real_world_target_verified=False,
        minimum_stock_accuracy_interval=dict(low=float(np.quantile(minima, .025)),
            high=float(np.quantile(minima, .975)), samples=bootstrap_samples, block_dates=block,
            method='exploratory_circular_market_date_block_bootstrap_of_minimum_stock_accuracy'),
        limitations=['Retrospective model selection and synthetic experiments cannot certify future accuracy.',
                     'An independent, frozen prospective test and verified point-in-time data remain required.',
                     'Blocks concatenate the supplied decision calendar, which may contain gaps.',
                     'Subset extrema concern average stock accuracy, not the probability all five are simultaneously correct.'])
