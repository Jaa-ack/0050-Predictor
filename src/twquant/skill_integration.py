"""Opt-in, development-only matched Trader Skills experiments; no promotion.

Only the existing fixed-ten shared model is fitted. C/D preserve A's complete
forecast panel; screening and risk affect paper allocation, never accuracy's
denominator. R is an independent daily breakout rule, not a probability model.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import time

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .comparison import reserve_development
from .direction_audit import audit_five_stock_accuracy
from .features import build_features, observed_market_sessions, select_feature_columns
from .ledger import json_safe
from .research import (run_research, simulate_portfolio, select_portfolio_candidates,
                       temporal_splits, _validate, _horizon_metrics)
from .skill_risk import RiskLimits, check_target_weights
from .skill_screener import screen, validate_spec
from .skill_signals import add_daily_signals, SIGNAL_COLUMNS, SIGNAL_VERSION
from .universe import TRAINING_TICKERS
from .workflows import data_kind, source_digest, write_json


DEFAULT_RULE_SPEC = dict(name='past-known daily trend and activity', missing='fail',
    rules=[dict(metric='f_price_ma20', op='>', value=0),
           dict(metric='f_rvol_20', op='>=', value=1)], rank=[])
UPSTREAM = dict(trader_skill='5189e79b9a480755a3733ac9713f5353981dbe18',
                day_trader_skill='3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd')


def _prediction_metrics(predictions):
    output = {}
    for horizon, group in predictions.groupby('horizon', sort=True):
        metrics = _horizon_metrics(group)
        known = group.actual_return.notna()
        y = group.loc[known, 'actual_return'].gt(0)
        metrics['roc_auc'] = float(roc_auc_score(y, group.loc[known, 'probability_up'])) if y.nunique() == 2 else None
        reliability = metrics.get('reliability', [])
        metrics['calibration_ece'] = (sum(b['count'] * abs(b['predicted'] - b['observed'])
            for b in reliability if b['count']) / len(y)) if len(y) else None
        metrics['per_fold'] = {str(f): _horizon_metrics(g) for f, g in group.groupby('fold')}
        output[str(horizon)] = metrics
    return output


def _paired_brier(a, b, seed):
    keys = ['date', 'ticker', 'horizon', 'fold']
    joined = a[keys + ['actual_return', 'probability_up']].merge(
        b[keys + ['actual_return', 'probability_up']], on=keys, validate='one_to_one', suffixes=('_a', '_b'))
    if len(joined) != len(a) or len(a) != len(b) or not np.array_equal(
            joined.actual_return_a, joined.actual_return_b, equal_nan=True):
        raise ValueError('Experiments must retain identical prediction keys and outcomes')
    output = {}
    for horizon, group in joined.groupby('horizon'):
        group = group.loc[np.isfinite(group.actual_return_a)].copy()
        if group.empty:
            output[str(horizon)] = dict(status='unavailable')
            continue
        labels = group.actual_return_a.gt(0).astype(float)
        group['difference'] = (group.probability_up_b - labels)**2 - (group.probability_up_a - labels)**2
        daily = group.groupby('date', sort=True).difference.mean().to_numpy()
        n = len(daily)
        requested = max(20, int(horizon))
        length = min(n, requested)
        rng = np.random.default_rng(seed)
        draws = []
        for _ in range(500):
            starts = rng.integers(n, size=int(np.ceil(n / length)))
            sample = ((starts[:, None] + np.arange(length)) % n).ravel()[:n]
            draws.append(float(daily[sample].mean()))
        output[str(horizon)] = dict(status='ok' if n >= requested else 'insufficient_dates_for_block_uncertainty', difference=float(daily.mean()),
            ci95=np.quantile(draws, [.025, .975]).tolist() if n >= requested else None, dates=n, observations=len(group),
            block_dates=length, requested_block_dates=requested, repetitions=500, lower_favors='B',
            limitation='Exploratory paired circular market-date blocks; short panels and gaps limit inference. Not an independent test.')
    return output


def _paper_decisions(predictions, development, config, spec, risk):
    p = predictions.loc[predictions.horizon.eq(1)].copy()
    if p.empty:
        empty = pd.DataFrame(columns=['date', 'ticker', 'eligible', 'weight'])
        return {'C': empty, 'D': empty.copy(), 'R': empty.assign(score=pd.Series(dtype=float))}, [], []
    metrics = {r['metric'] for r in [*spec.get('rules', []), *spec.get('rank', [])]}
    inputs = development.loc[development.ticker.isin(TRAINING_TICKERS),
                             ['date', 'ticker', *sorted(metrics | {'f_volatility_20', 'f_scan_breakout_20d'})]]
    point = p[['date', 'ticker']].merge(inputs, on=['date', 'ticker'], validate='one_to_one', how='left')
    checks, passed = [], {}
    for date, group in point.groupby('date', sort=True):
        for result in screen(group[['ticker', *sorted(metrics)]], spec):
            checks.append(dict(date=date, **result))
            passed[(date, result['ticker'])] = result['passed']
    weight = min(config.max_weight, 1 / config.max_positions)
    c = p[['date', 'ticker']].copy()
    c['eligible'] = [passed[(d, t)] for d, t in c.itertuples(index=False, name=None)]
    c['weight'] = weight
    d = c.copy()
    d['eligible'] = False
    d['weight'] = 0.
    risk_audit = []
    allocated = min(weight, risk.max_weight, risk.max_exposure / config.max_positions,
                    (1 - risk.min_cash) / config.max_positions, risk.max_capital_at_risk / config.max_positions)
    selected_input = p.merge(c[['date', 'ticker', 'eligible']], on=['date', 'ticker'], validate='one_to_one')
    selected_input = selected_input.rename(columns={'eligible': '_decision_eligible'})
    for date, group in selected_input.groupby('date', sort=True):
        selected = select_portfolio_candidates(group, config)
        weights = {t: allocated for t in selected.ticker}
        vols = point.loc[point.date.eq(date)].set_index('ticker').f_volatility_20.to_dict()
        verdict = check_target_weights(weights, vols, risk)
        risk_audit.append(dict(date=date, proposed_weights=weights, **verdict))
        if verdict['verdict'] == 'PASS':
            mask = d.date.eq(date) & d.ticker.isin(weights)
            d.loc[mask, 'eligible'] = True
            d.loc[mask, 'weight'] = allocated
    rule = point[['date', 'ticker']].copy()
    rule['eligible'] = point.f_scan_breakout_20d.eq(1)
    rule['weight'] = weight
    rule['score'] = rule.eligible.astype(float)
    return {'C': c, 'D': d, 'R': rule}, checks, risk_audit


def compare_skill_modules(prices, config, *, external=None, holdout_dates=126,
                          mode='postclose', rule_spec=None, risk_limits=None, output=None):
    """Fit A/B under identical existing folds, then evaluate independent C/D/R.

    No test-driven parameter search, holdout release, latest-model refit or
    current/champion pointer writes. Output must be a fresh directory; protocol
    is written before fitting. Previously inspected history stays exploratory.
    """
    started = time.perf_counter()
    spec = DEFAULT_RULE_SPEC if rule_spec is None else rule_spec
    risk = risk_limits or RiskLimits()
    validate_spec(spec)
    frame = build_features(prices, external=external, mode=mode)
    baseline_cols = select_feature_columns(frame, config.feature_profile)
    augmented = add_daily_signals(frame, observed_market_sessions(prices))
    metrics = {r['metric'] for r in [*spec.get('rules', []), *spec.get('rank', [])]}
    if not metrics <= set(baseline_cols) | set(SIGNAL_COLUMNS):
        raise ValueError('Study rules must use existing decision-time model features or scanner signals')
    development, start = reserve_development(augmented, config.horizons, holdout_dates)
    synthetic_external = external is not None and 'data_kind' in external and external.data_kind.astype(str).str.contains('synthetic|demo', case=False).any()
    kind = 'synthetic' if synthetic_external else data_kind(prices)
    folder = Path(output) if output is not None else None
    external_hash = hashlib.sha256(pd.util.hash_pandas_object(external, index=False).to_numpy().tobytes()).hexdigest() if external is not None else None
    protocol = dict(version='trader-skills-study-v1', upstream=UPSTREAM,
        signal_version=SIGNAL_VERSION, config=asdict(config), mode=mode,
        rule_spec=spec, risk_limits=asdict(risk), training_tickers=list(TRAINING_TICKERS),
        source_sha256=source_digest(), data_kind=kind, holdout_dates=holdout_dates,
        holdout_start=str(start.date()), holdout_opened=False,
        price_sha256=hashlib.sha256(pd.util.hash_pandas_object(prices, index=False).to_numpy().tobytes()).hexdigest(),
        external_sha256=external_hash, snapshot_id=prices.attrs.get('snapshot_id'),
        availability_bases=sorted(prices.availability_basis.astype(str).unique()) if 'availability_basis' in prices else ['unknown'],
        baseline_features=baseline_cols, added_features=list(SIGNAL_COLUMNS),
        rule_strategy='R: closing-high breakout above all prior 20 session closes; equal weights; ticker ties; no model threshold.',
        decision_risk_policy='D: select C on decision-time scores, cap equal committed capital, atomically block unsafe daily plans; no replacement after failed fill.',
        evidence='Exploratory already-inspected historical development dates or synthetic mechanics; no live or independent final-test evidence.')
    if folder:
        folder.mkdir(parents=True, exist_ok=False)
        write_json(folder / 'protocol.json', protocol)
    variants, results, timings = {}, {}, {}
    for name, columns in [('A', baseline_cols), ('B', baseline_cols + list(SIGNAL_COLUMNS))]:
        tick = time.perf_counter()
        result = run_research(development, columns, config)
        timings[name] = time.perf_counter() - tick
        results[name] = result
        metrics_by_h = _prediction_metrics(result.predictions)
        audit = {}
        validated = _validate(development, baseline_cols, config)
        for h in config.horizons:
            splits = temporal_splits(validated, h, config)
            last = validated.loc[validated[f'benchmark_target_{h}'].notna(), 'date'].max()
            expected = sorted({d for split in splits for d in split['test_calendar'] if d <= last})
            audit[str(h)] = audit_five_stock_accuracy(result.predictions.loc[result.predictions.horizon.eq(h)],
                evidence_kind=kind, expected_dates=expected, bootstrap_samples=200,
                block_dates=max(20, h), seed=config.random_state)
        regimes = result.predictions.merge(development[['date', 'ticker', 'f_market_return_20']],
                                          on=['date', 'ticker'], how='left', validate='many_to_one')
        regimes['regime'] = np.where(regimes.f_market_return_20.ge(0), 'trailing_market_nonnegative', 'trailing_market_negative')
        regimes.loc[regimes.f_market_return_20.isna(), 'regime'] = 'unknown'
        variants[name] = dict(prediction_source=name, prediction_metrics=metrics_by_h,
            direction_audit=audit, portfolio=result.metrics['portfolio'],
            market_regimes={str(r): _prediction_metrics(g) for r, g in regimes.groupby('regime')},
            transfer=result.metrics['transfer'])
        if folder:
            dest = folder / name
            dest.mkdir()
            result.predictions.to_parquet(dest / 'predictions.parquet', index=False)
            result.transfer_predictions.to_parquet(dest / 'transfer_predictions.parquet', index=False)
            result.folds.to_csv(dest / 'folds.csv', index=False)
            result.equity.to_csv(dest / 'equity.csv', index=False)
    paired = _paired_brier(results['A'].predictions, results['B'].predictions, config.random_state)
    plans, checks, risk_audit = _paper_decisions(results['A'].predictions, development, config, spec, risk)
    for name, decisions in plans.items():
        tick = time.perf_counter()
        equity, portfolio = simulate_portfolio(results['A'].predictions, config, decisions=decisions, rule_only=name == 'R')
        timings[name] = time.perf_counter() - tick
        variants[name] = dict(prediction_source='A' if name != 'R' else None,
            prediction_metrics=variants['A']['prediction_metrics'] if name != 'R' else None,
            direction_audit=variants['A']['direction_audit'] if name != 'R' else None,
            portfolio=portfolio, eligible_rows=int(decisions.eligible.sum()))
        if folder:
            dest = folder / name
            dest.mkdir()
            decisions.to_parquet(dest / 'decisions.parquet', index=False)
            equity.to_csv(dest / 'equity.csv', index=False)
    report = dict(**protocol, variants=variants, paired_brier_B_minus_A=paired,
        screening=dict(rows=len(checks), passed=sum(c['passed'] for c in checks)),
        risk=dict(days=len(risk_audit), blocked=sum(r['verdict'] == 'BLOCK' for r in risk_audit)),
        timing_seconds=timings, elapsed_seconds=time.perf_counter() - started,
        limitations=['C/D accuracy and calibration are identical to A; abstention cannot satisfy daily direction targets.',
            'Only h1 has executable portfolio accounting. 5/20-session strategy NAV and rebalance experiments are not implemented.',
            'Daily opening fills, fixed fractional capital, no order book, market impact or guaranteed stops.',
            'Risk thresholds and rule choices are hypotheses fixed before comparison, not calibrated guarantees.',
            'Circular market-date blocks retain overlapping-label dependence but do not establish independent-test significance.'])
    report = json_safe(report)
    if folder:
        write_json(folder / 'screen_checks.json', checks)
        write_json(folder / 'risk_checks.json', risk_audit)
        write_json(folder / 'report.json', report)
        write_json(folder / 'artifact_hashes.json', {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()})
    return report
