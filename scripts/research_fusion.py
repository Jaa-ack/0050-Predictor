"""Offline matched source-fusion study; accepts trusted local feature snapshots.

Uses no provider credentials, network, final-holdout labels, or model promotion.
Run with the project Python environment from the repository root.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from twquant.cli import config_from
from twquant.comparison import reserve_development
from twquant.direction import FUSION_CANDIDATES, search_direction_models
from twquant.direction_audit import load_membership_snapshot
from twquant.direction_fusion import FUSION_RECIPE
from twquant.universe import TRAINING_TICKERS
from twquant.workflows import source_digest, write_json


FIELDS = ('overall_accuracy', 'minimum_stock_accuracy', 'worst_five_accuracy',
          'coverage', 'test_dates', 'empirical_target_met')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def independent_panel(rows, dates, tickers):
    grid = pd.MultiIndex.from_product([dates, tickers], names=['date', 'ticker'])
    panel = rows.set_index(['date', 'ticker']).reindex(grid)
    known = np.isfinite(panel.actual_return)
    valid = panel.probability_up.between(0, 1) & panel.decision_threshold.between(0, 1)
    correct = known & valid & panel.probability_up.ge(panel.decision_threshold).eq(panel.actual_return.gt(0))
    stocks = correct.groupby(level='ticker').mean().sort_values()
    return panel, correct, dict(overall_accuracy=float(correct.mean()), minimum_stock_accuracy=float(stocks.min()),
                               worst_five_accuracy=float(stocks.iloc[:5].mean()), coverage=float((known & valid).mean()))


def paired(a, b, dates, tickers, horizon):
    left, correct_a, _ = independent_panel(a, dates, tickers)
    right, correct_b, _ = independent_panel(b, dates, tickers)
    np.testing.assert_array_equal(left.actual_return, right.actual_return)
    daily = (correct_a.astype(float) - correct_b.astype(float)).groupby(level='date').mean().to_numpy()
    length = min(len(daily), max(20, horizon))
    rng = np.random.default_rng(42)
    draws = []
    for _ in range(1000):
        starts = rng.integers(len(daily), size=int(np.ceil(len(daily) / length)))
        indices = ((starts[:, None] + np.arange(length)) % len(daily)).ravel()[:len(daily)]
        draws.append(daily[indices].mean())
    known = left.actual_return.notna() & left.probability_up.notna() & right.probability_up.notna()
    labels = left.loc[known, 'actual_return'].gt(0).astype(float)
    return dict(accuracy_delta=float(daily.mean()), ci95=np.quantile(draws, [.025, .975]).tolist(),
                block_dates=length, repetitions=1000, unit='market_date',
                brier_delta_known_rows=float(((left.loc[known, 'probability_up'] - labels) ** 2
                                             - (right.loc[known, 'probability_up'] - labels) ** 2).mean()),
                known_rows=int(known.sum()), expected_rows=len(left), exploratory=True,
                limitation='No multiple-comparison or repeated-history correction; not independent future evidence.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('configs/research_fusion.json'))
    parser.add_argument('--membership', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--holdout-dates', type=int, default=126)
    parser.add_argument('--mode', choices=['postclose', 'preopen'], default='preopen')
    parser.add_argument('--evidence-kind', choices=['historical', 'synthetic', 'unknown'], required=True)
    parser.add_argument('--information-start', help='Optional predeclared secondary diagnostic start; never replaces full audit')
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('Use a fresh study directory; existing evidence cannot be overwritten')
    frame = pd.read_parquet(args.frame)
    config = config_from(args.config)
    membership = load_membership_snapshot(args.membership) if args.membership else None
    if membership:
        config = replace(config, evaluation_tickers=tuple(dict.fromkeys([
            *config.evaluation_tickers, *(t for t in membership['tickers'] if t not in TRAINING_TICKERS)])))
    inputs = {str(p): sha(p) for p in [args.frame, args.config, *([args.membership] if args.membership else [])]}
    development, boundary = reserve_development(frame, config.horizons, args.holdout_dates)
    dates = pd.DatetimeIndex(development.loc[development.ticker.isin((*TRAINING_TICKERS, '0050')), 'date'].unique()).sort_values()
    arms = dict(controls=FUSION_CANDIDATES[:3], equal=(FUSION_CANDIDATES[3],),
                learned=(FUSION_CANDIDATES[4],), no_us=(FUSION_CANDIDATES[4],),
                no_night=(FUSION_CANDIDATES[4],), selection=FUSION_CANDIDATES)
    protocol = dict(schema_version=1, source_sha256=source_digest(), runner_sha256=sha(__file__),
                    input_files=inputs, config=asdict(config), mode=args.mode, evidence_kind=args.evidence_kind,
                    arms={name: [asdict(c) for c in cs] for name, cs in arms.items()}, fusion_recipe=FUSION_RECIPE,
                    ablations={'no_us': 'remove all f_ext_fmp_ features', 'no_night': 'remove all f_ext_taifex_ features'},
                    selection_policy='pooled_brier', thresholds=[.5], holdout_dates=args.holdout_dates,
                    holdout_start=str(boundary.date()), holdout_opened=False,
                    information_start=args.information_start, training_tickers=list(TRAINING_TICKERS),
                    snapshot_id=frame.attrs.get('snapshot_id'),
                    limitation='Repeated historical development, current membership, latest adjustments and reconstructed receipts. '
                               'Six declared arms, no outcome-based winner promotion. News and intraday are separate future work.')
    write_json(args.out / 'protocol.json', protocol)  # freeze before any model fit
    results, calendars, verification = {}, {}, {}
    for name, candidates in arms.items():
        data = frame
        prefix = {'no_us': 'f_ext_fmp_', 'no_night': 'f_ext_taifex_'}.get(name)
        if prefix:
            data = frame.drop(columns=[c for c in frame if c.startswith(prefix)])
        result = search_direction_models(data, config, candidates=candidates, holdout_dates=args.holdout_dates,
                    evidence_kind=args.evidence_kind, bootstrap_samples=100, output=args.out / name,
                    selection_policy='pooled_brier', mode=args.mode,
                    constituents=membership['tickers'] if membership else None,
                    membership_as_of=membership['as_of'] if membership else None)
        results[name] = result
        for h in config.horizons:
            intervals = [s for s in result.report['selections'] if s['horizon'] == h]
            calendar = dates[np.logical_or.reduce([dates.to_series().between(s['test_start'], s['test_end']).to_numpy()
                                                  for s in intervals])]
            if h in calendars:
                np.testing.assert_array_equal(calendar, calendars[h])
            calendars[h] = calendar
            panels = [('core', result.predictions, TRAINING_TICKERS, result.report['by_horizon'][str(h)])]
            if membership:
                all_rows = pd.concat([result.predictions, result.transfer_predictions], ignore_index=True)
                panels.append(('full', all_rows.loc[all_rows.ticker.isin(membership['tickers'])],
                               membership['tickers'], result.report['constituents']['by_horizon'][str(h)]))
            for scope, rows, tickers, audit in panels:
                _, _, manual = independent_panel(rows.loc[rows.horizon.eq(h)], calendar, tickers)
                assert audit['test_dates'] == len(calendar)
                for key, value in manual.items():
                    assert np.isclose(audit[key], value, atol=1e-12, rtol=0), (name, scope, h, key)
            all_rows = pd.concat([result.predictions, result.transfer_predictions], ignore_index=True)
            for field in ['model_version', 'decision_threshold']:
                assert all_rows.groupby(['date', 'horizon'])[field].nunique().le(1).all()
        verification[name] = dict(manual_complete_grid_scores_match=True, shared_models_and_thresholds=True)
        print(json.dumps(dict(arm=name, complete=True, real_world_target_verified=False)), flush=True)
    controls = {c.name for c in FUSION_CANDIDATES[:3]}
    for a, b in zip(results['selection'].report['selections'], results['controls'].report['selections'], strict=True):
        assert [s for s in a['candidate_scores'] if s['candidate'] in controls] == b['candidate_scores']
    summary = dict(protocol=protocol, arms={}, paired={}, verification=verification, real_world_target_verified=False)
    for name, result in results.items():
        report = result.report
        summary['arms'][name] = dict(core={h: {k: a[k] for k in FIELDS} for h, a in report['by_horizon'].items()})
        if membership:
            summary['arms'][name]['full'] = {h: {k: a[k] for k in FIELDS}
                                             for h, a in report['constituents']['by_horizon'].items()}
        if args.information_start and membership:
            ready = {}
            all_rows = pd.concat([result.predictions, result.transfer_predictions], ignore_index=True)
            for h, calendar in calendars.items():
                ready_dates = calendar[calendar >= pd.Timestamp(args.information_start)]
                if len(ready_dates):
                    rows = all_rows.loc[all_rows.horizon.eq(h) & all_rows.date.isin(ready_dates)
                                        & all_rows.ticker.isin(membership['tickers'])]
                    _, _, ready[str(h)] = independent_panel(rows, ready_dates, membership['tickers'])
                    ready[str(h)]['test_dates'] = len(ready_dates)
            summary['arms'][name]['information_ready'] = ready
    for newer, older in [('selection', 'controls'), ('learned', 'equal'), ('learned', 'no_us'), ('learned', 'no_night')]:
        comparison = {}
        for h, calendar in calendars.items():
            for scope, tickers in [('core', TRAINING_TICKERS), *([('full', membership['tickers'])] if membership else [])]:
                panels = []
                for name in [newer, older]:
                    result = results[name]
                    all_rows = pd.concat([result.predictions, result.transfer_predictions], ignore_index=True)
                    panels.append(all_rows.loc[all_rows.horizon.eq(h) & all_rows.ticker.isin(tickers)])
                comparison[f'{scope}:{h}'] = paired(*panels, calendar, tickers, h)
        summary['paired'][f'{newer}_minus_{older}'] = comparison
    assert source_digest() == protocol['source_sha256'] and sha(__file__) == protocol['runner_sha256']
    assert all(sha(p) == value for p, value in inputs.items())
    summary['verification'].update(source_and_inputs_unchanged=True, matched_control_calibrations_exact=True)
    write_json(args.out / 'comparison.json', summary)
    print(json.dumps(dict(study_complete=True, comparisons=str(args.out / 'comparison.json'),
                          real_world_target_verified=False)))


if __name__ == '__main__':
    main()
