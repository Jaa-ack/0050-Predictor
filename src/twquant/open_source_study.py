"""Independent postclose experiments; never writes models, ledgers or pointers.

Run with ``python -m twquant.open_source_study --help``. A fresh output directory
is mandatory. Hypotheses, source/config/data hashes and splits precede fitting;
no candidate is selected or promoted from these already-inspected dates.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import time

import numpy as np
import pandas as pd

from .data import PriceStore, validate_prices
from .direction_audit import audit_five_stock_accuracy
from .features import build_features, observed_market_sessions, select_feature_columns
from .ledger import json_safe
from .open_source_factors import ALPHA_COLUMNS, add_alpha_subset, causal_feature_audit
from .open_source_intervals import fit_interval
from .research import (ResearchConfig, _fit, _horizon_metrics, _validate,
                       predict_evaluation, select_portfolio_candidates,
                       simulate_portfolio, temporal_splits, transfer_metrics)
from .skill_integration import _paired_brier
from .universe import TRAINING_TICKERS
from .workflows import data_kind, load_events, source_digest, write_json


def research_features(prices):
    return add_alpha_subset(build_features(prices), observed_market_sessions(prices))


def _frame_hash(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes()).hexdigest()


def _metrics(predictions):
    result = {}
    for horizon, group in predictions.groupby('horizon'):
        metrics = _horizon_metrics(group)
        known = group[group.actual_return.notna()]
        if len(known):
            # Proper interval score on simple-return scale, alpha=.2.
            y, lo, hi = known.actual_return, known.q10, known.q90
            score = hi - lo + 10 * (lo - y).clip(lower=0) + 10 * (y - hi).clip(lower=0)
            metrics['interval_score80'] = float(score.mean())
        else:
            metrics['interval_score80'] = None
        metrics['interval_method'] = 'See variant and per-fold interval_parameters in this study'
        metrics['per_fold'] = {str(f): _horizon_metrics(g) for f, g in group.groupby('fold')}
        result[str(horizon)] = metrics
    return result


def _simple_baselines(predictions):
    result = {}
    for h, group in predictions.groupby('horizon'):
        p = group[group.actual_return.notna()]
        y = p.actual_return.gt(0).to_numpy()
        actual = p.actual_return.to_numpy()
        result[str(h)] = {}
        for name, probability, center in [
            ('linear', p.baseline_probability, p.baseline_return),
            ('frequency', p.training_probability, p.training_mean_return),
            ('p50_zero', np.full(len(p), .5), np.zeros(len(p)))]:
            result[str(h)][name] = dict(observations=len(p),
                accuracy=float(np.mean((np.asarray(probability) >= .5) == y)) if len(p) else None,
                brier=float(np.mean((np.asarray(probability) - y)**2)) if len(p) else None,
                mae=float(np.mean(np.abs(np.asarray(center) - actual))) if len(p) else None)
    return result


def _cost_stress(predictions, config, folder):
    """Freeze base-cost decisions before changing slippage, so trades match."""
    decisions = predictions.loc[predictions.horizon.eq(1), ['date', 'ticker']].copy()
    decisions['eligible'], decisions['weight'] = False, 0.
    score = 'arithmetic_expected_return' if 'arithmetic_expected_return' in predictions else 'expected_return'
    decisions['score'] = predictions.loc[decisions.index, score].fillna(0.)
    for _, group in predictions[predictions.horizon.eq(1)].groupby('date'):
        selected = select_portfolio_candidates(group, config)
        if len(selected):
            mask = decisions.index.isin(selected.index)
            decisions.loc[mask, 'eligible'] = True
            decisions.loc[mask, 'weight'] = min(config.max_weight, 1 / config.max_positions)
    stress = {}
    for name, multiplier in [('base', 1), ('double_slippage', 2)]:
        cfg = replace(config, slippage=config.slippage * multiplier)
        equity, metrics = simulate_portfolio(predictions, cfg, decisions=decisions, rule_only=True)
        equity.to_csv(folder / f'equity_{name}.csv', index=False)
        stress[name] = metrics
    decisions.to_parquet(folder / 'frozen_decisions.parquet', index=False)
    return stress


def run_study(prices, config, *, holdout_dates=126, external=None, output):
    """A: factors; B: residual intervals + pooled control; C: prefix audit.

    Uses the existing model fitter and maturity-purged folds. The explicit
    fixed-ten/0050 calendar retains entirely missing stock days in the audit.
    All future labels, including unknown answers, share the same scoring grid.
    Only fixed-ten data fit transforms/models/calibration; transfer is read-only.
    """
    if config.objective != 'close_return' or not set(config.horizons) <= {1, 5, 20}:
        raise ValueError('Study requires postclose close_return horizons 1/5/20')
    if len(set(config.horizons)) != len(config.horizons) or not config.horizons:
        raise ValueError('Distinct nonempty study horizons required')
    if isinstance(holdout_dates, bool) or not isinstance(holdout_dates, int) or holdout_dates < 1:
        raise ValueError('Positive holdout dates required')
    if config.max_folds < 1:
        raise ValueError('Positive max_folds required')
    started = time.perf_counter()
    snapshot_id = prices.attrs.get('snapshot_id')
    snapshot_sha = prices.attrs.get('manifest', {}).get('sha256')
    prices = validate_prices(prices)
    calendar = observed_market_sessions(prices)
    if len(calendar) <= holdout_dates:
        raise ValueError('Insufficient history before reserved holdout')
    holdout_start = calendar[-holdout_dates]
    development_calendar = calendar[calendar < holdout_start]
    # Feature schema is fixed from input; no test labels choose features.
    frame = research_features(prices)
    if external is not None:
        from .features import point_in_time_join
        frame = point_in_time_join(frame, external)
    baseline = [c for c in select_feature_columns(frame, config.feature_profile) if c not in ALPHA_COLUMNS]
    development = frame[frame.date < holdout_start].copy()
    freeze = holdout_start.tz_localize('Asia/Taipei').tz_convert('UTC')
    for h in config.horizons:
        known = (pd.to_datetime(development[f'label_available_at_{h}'], utc=True).lt(freeze)
                 & development[f'label_end_{h}'].lt(holdout_start))
        for prefix in ('target', 'execution_target', 'trade_return', 'benchmark_target',
                       'benchmark_trade_return', 'benchmark_end_adj_close'):
            development.loc[~known, f'{prefix}_{h}'] = np.nan
        development.loc[~known, f'trade_valid_{h}'] = False
    data = _validate(development, baseline, config)
    evaluation = _validate(development, baseline, config, training=False)
    # Benchmark availability must not remove a fixed-ten outcome or test date.
    split_data = data.drop(columns=[f'benchmark_target_{h}' for h in config.horizons])
    splits = {h: temporal_splits(split_data, h, config, calendar_dates=development_calendar)
              for h in config.horizons}
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=False)
    kind = data_kind(prices)
    if external is not None and 'data_kind' in external and external.data_kind.astype(str).str.contains('synthetic|demo', case=False).any():
        kind = 'synthetic'
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = 'unavailable'
    protocol = dict(version='open-source-study-v1', created_at=datetime.now(timezone.utc).isoformat(),
        hypotheses=dict(A='Nine Alpha158-inspired factors may add price/volume shape information.',
            B='Known trailing volatility may improve interval score at similar coverage; pooled exact-rank control separates the quantile convention.',
            C='Prefix recalculation should preserve all past feature values and missing-value masks.'),
        config=asdict(config), mode='postclose', decision_time='20:00 Asia/Taipei',
        target='log adjusted close(t+h)/adjusted close(t); not intraday or executable return',
        horizons=list(config.horizons), data_kind=kind, training_tickers=list(TRAINING_TICKERS),
        holdout_start=str(holdout_start.date()), holdout_dates=holdout_dates, holdout_opened=False,
        price_sha256=_frame_hash(prices), snapshot_id=snapshot_id, snapshot_file_sha256=snapshot_sha,
        external_sha256=_frame_hash(external) if external is not None else None,
        availability_bases=sorted(prices.availability_basis.unique()),
        source_sha256=source_digest(), parent_git_revision=revision,
        baseline_features=baseline, added_features=list(ALPHA_COLUMNS),
        environment=dict(python=platform.python_version(), platform=platform.platform(),
            packages={p: importlib.metadata.version(p) for p in ['numpy', 'pandas', 'scipy', 'scikit-learn', 'pyarrow']}),
        selection='None: fixed hypotheses; no hyperparameter search or promotion. All arms retained.',
        evidence='Exploratory previously-inspected historical dates or synthetic mechanics, never prospective evidence.')
    write_json(folder / 'protocol.json', protocol)
    source_folder = folder / 'source'
    source_folder.mkdir()
    for path in Path(__file__).parent.glob('*.py'):
        shutil.copyfile(path, source_folder / path.name)
    write_json(folder / 'splits.json', {str(h): [{k: (v if k != 'test_calendar' else [str(d.date()) for d in v])
        for k, v in s.items()} for s in entries] for h, entries in splits.items()})
    development.to_parquet(folder / 'features.parquet', index=False)
    # Only development-price prefixes; reserved outcomes do not inform this audit.
    audit_prices = prices[prices.date < holdout_start]
    cutoffs = [development_calendar[int((len(development_calendar) - 1) * fraction)] for fraction in (.5, .75, .9)]
    causality = causal_feature_audit(audit_prices, research_features, cutoffs)
    write_json(folder / 'causality.json', causality)
    if not causality['passed']:
        raise ValueError('Feature prefix audit failed; inspect causality.json')
    names = ('current', 'alpha', 'pooled_exact', 'vol_scaled')
    outputs, transfers = {n: [] for n in names}, {n: [] for n in names}
    fold_rows, interval_parameters, timings = [], [], []
    for h, entries in splits.items():
        for fold, split in enumerate(entries, 1):
            train, cal, test = (data.loc[split[k]] for k in ('train', 'calibration', 'test'))
            ext = evaluation[evaluation.date.isin(split['test_calendar'])]
            for name, columns in [('current', baseline), ('alpha', baseline + list(ALPHA_COLUMNS))]:
                tick = time.perf_counter()
                model = _fit(train, cal, columns, h, config)
                # Include only genuinely fitted training rows in fallback scales/priors.
                fitted_train = train[train.date >= pd.Timestamp(model.training_start)]
                prediction = predict_evaluation(model, test, h)
                prediction['fold'] = fold
                prediction['training_mean_return'] = float(np.expm1(fitted_train[f'target_{h}']).mean())
                outputs[name].append(prediction)
                ext_prediction = predict_evaluation(model, ext, h) if not ext.empty else pd.DataFrame()
                if not ext_prediction.empty:
                    ext_prediction['fold'] = fold
                    transfers[name].append(ext_prediction)
                if name == 'current':
                    cal_prediction = model.predict(cal)
                    for interval_name, scaled in [('pooled_exact', False), ('vol_scaled', True)]:
                        interval = fit_interval(fitted_train, cal, cal_prediction, h, scaled=scaled)
                        outputs[interval_name].append(interval.apply(test, prediction))
                        if not ext_prediction.empty:
                            transfers[interval_name].append(interval.apply(ext, ext_prediction))
                        interval_parameters.append(dict(variant=interval_name, fold=fold, **asdict(interval)))
                row = dict(variant=name, horizon=h, fold=fold, model_version=model.model_version,
                    train_start=model.training_start, train_end=train.date.max(), train_rows=model.training_rows,
                    train_label_end=train[f'label_end_{h}'].max(),
                    train_label_available_max=pd.to_datetime(train[f'label_available_at_{h}'], utc=True).max(),
                    calibration_start=cal.date.min(), calibration_end=cal.date.max(),
                    calibration_label_end=cal[f'label_end_{h}'].max(),
                    calibration_label_available_max=pd.to_datetime(cal[f'label_available_at_{h}'], utc=True).max(),
                    test_start=min(split['test_calendar']), test_end=max(split['test_calendar']),
                    test_rows=len(test), expected_rows=len(split['test_calendar']) * len(TRAINING_TICKERS))
                fold_rows.append(row)
                timing = dict(variant=name, horizon=h, fold=fold, elapsed_seconds=time.perf_counter() - tick)
                timings.append(timing)
                with (folder / 'execution.jsonl').open('a') as handle:
                    handle.write(json.dumps(timing) + '\n')
    predictions = {n: pd.concat(outputs[n], ignore_index=True) for n in names}
    variants = {}
    for name, prediction in predictions.items():
        dest = folder / name
        dest.mkdir()
        prediction.to_parquet(dest / 'predictions.parquet', index=False)
        transfer = pd.concat(transfers[name], ignore_index=True) if transfers[name] else pd.DataFrame()
        transfer.to_parquet(dest / 'transfer_predictions.parquet', index=False)
        metrics = _metrics(prediction)
        audits = {str(h): audit_five_stock_accuracy(prediction[prediction.horizon.eq(h)],
            evidence_kind=kind, expected_dates=sorted({d for s in splits[h] for d in s['test_calendar']}),
            bootstrap_samples=200, block_dates=max(20, h), seed=config.random_state) for h in config.horizons}
        variants[name] = dict(prediction_metrics=metrics, direction_audit=audits,
            transfer=transfer_metrics(transfer),
            portfolio=_cost_stress(prediction, config, dest) if name in ('current', 'alpha') else None,
            point_prediction_source='alpha' if name == 'alpha' else 'current')
    pd.DataFrame(fold_rows).to_csv(folder / 'folds.csv', index=False)
    report = json_safe(dict(**protocol, variants=variants, causality=causality,
        interval_parameters=interval_parameters, timing=timings, elapsed_seconds=time.perf_counter() - started,
        paired_brier_alpha_minus_current=_paired_brier(predictions['current'], predictions['alpha'], config.random_state),
        simple_baselines=_simple_baselines(predictions['current']),
        limitations=['Final reserved dates remain excluded; historical exploration is not an untouched test.',
            'No claim about 60/300-second first change, full 50 constituents or live predictive advantage.',
            'Interval changes do not improve point accuracy or strategy returns; overlapping labels and stocks are dependent.',
            'Prefix audit samples cutoffs and features, not all revisions, model training or external event delivery.',
            'Only h1 has strategy accounting; fixed costs/open fills omit market impact and queue capacity.']))
    write_json(folder / 'report.json', report)
    write_json(folder / 'artifact_hashes.json', {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob('*')) if p.is_file()})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Existing read-only price workspace')
    parser.add_argument('--snapshot', help='Explicit immutable price snapshot; defaults to latest')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--holdout-dates', type=int, default=126)
    parser.add_argument('--out', type=Path, required=True, help='Fresh isolated output directory')
    args = parser.parse_args(argv)
    prices = PriceStore(args.root / 'data').read_prices(args.snapshot)
    cfg = ResearchConfig(**json.loads(args.config.read_text()))
    report = run_study(prices, cfg, external=load_events(args.events),
                       holdout_dates=args.holdout_dates, output=args.out)
    print(json.dumps(dict(output=str(args.out), data_kind=report['data_kind'],
        holdout_opened=report['holdout_opened'], elapsed_seconds=report['elapsed_seconds']), ensure_ascii=False))


if __name__ == '__main__':
    main()
