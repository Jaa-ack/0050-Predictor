"""Direction research with a fixed universe, full coverage and explicit evidence labels.

This module does not promote a model, open a final holdout, or issue forecasts.
Zero returns belong to the non-up class, as in the existing research engine.
"""
from __future__ import annotations

from itertools import combinations
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .universe import TRAINING_TICKERS, asset_class


def audit_five_stock_accuracy(predictions, *, evidence_kind='unknown', expected_dates=None,
                             target_accuracy=.75, min_dates=126, bootstrap_samples=500,
                             block_dates=20, seed=42):
    """Score every stock and all 252 five-stock subsets on the same calendar.

    Missing/abstained predictions and unresolved outcomes cannot be deleted to
    improve accuracy. Bootstrap intervals resample whole market-date blocks;
    they are exploratory diagnostics, not a certification of future accuracy.
    """
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
    if not set(p.ticker) <= set(TRAINING_TICKERS):
        raise ValueError('Direction audit universe must be the fixed ten')
    if p.duplicated(['date', 'ticker']).any():
        raise ValueError('Direction predictions contain duplicate ticker/date rows')
    dates = pd.DatetimeIndex(p.date.unique() if expected_dates is None else expected_dates).normalize().unique().sort_values()
    if not len(dates) or dates.hasnans or not set(p.date) <= set(dates):
        raise ValueError('Invalid expected decision dates')
    grid = pd.MultiIndex.from_product([dates, TRAINING_TICKERS], names=['date', 'ticker'])
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
    correct = (known & valid & predicted_up.eq(up)).to_numpy().reshape(len(dates), 10)
    stocks = {}
    for ticker in TRAINING_TICKERS:
        mask = grid.get_level_values('ticker') == ticker
        k, v, y, decision = known[mask], valid[mask], up[mask], predicted_up[mask]
        successes = (k & v & decision.eq(y))
        recall_up = float((v[y & k] & decision[y & k]).mean()) if (y & k).any() else None
        recall_down = float((v[~y & k] & ~decision[~y & k]).mean()) if (~y & k).any() else None
        balanced = (recall_up + recall_down) / 2 if recall_up is not None and recall_down is not None else None
        stocks[ticker] = dict(accuracy=float(successes.sum() / len(dates)),
            balanced_accuracy=balanced, observations=int(k.sum()), expected_predictions=len(dates),
            coverage=float((k & v).sum() / len(dates)), recall_up=recall_up, recall_down=recall_down,
            always_up_accuracy=float((k & y).sum() / len(dates)),
            always_down_accuracy=float((k & ~y).sum() / len(dates)))
    subsets = []
    for indices in combinations(range(10), 5):
        values = correct[:, indices]
        subsets.append(dict(tickers=[TRAINING_TICKERS[i] for i in indices],
                            accuracy=float(values.mean()), all_five_correct_rate=float(values.all(axis=1).mean())))
    subsets.sort(key=lambda s: (s['accuracy'], s['tickers']))
    minimum = min(s['accuracy'] for s in stocks.values())
    edge = all(s['balanced_accuracy'] is not None and s['balanced_accuracy'] > .5
               and s['accuracy'] > max(s['always_up_accuracy'], s['always_down_accuracy']) for s in stocks.values())
    coverage = float((known & valid).mean())
    empirical = bool(minimum >= target_accuracy and coverage == 1 and len(dates) >= min_dates and edge)
    # Preserve cross-stock dependence and at least the label-overlap scale.
    block = min(len(dates), max(block_dates, int(predictions.horizon.iloc[0])))
    rng = np.random.default_rng(seed)
    minima = []
    for _ in range(bootstrap_samples):
        starts = rng.integers(len(dates), size=int(np.ceil(len(dates) / block)))
        sample = ((starts[:, None] + np.arange(block)) % len(dates)).ravel()[:len(dates)]
        minima.append(float(correct[sample].mean(axis=0).min()))
    return dict(evidence_kind=evidence_kind, horizon=int(predictions.horizon.iloc[0]),
        target_accuracy=target_accuracy, training_tickers=list(TRAINING_TICKERS),
        scope='all_252_five_stock_subsets_of_fixed_ten_only',
        definition='Every fixed-ten stock must reach the target, with full coverage; each five-stock subset is also reported. Five simultaneously correct is a separate metric.',
        test_dates=len(dates), expected_predictions=len(grid), missing_predictions=int((~present).sum()),
        unresolved_labels=int((~known).sum()), coverage=coverage, overall_accuracy=float(correct.mean()),
        minimum_stock_accuracy=minimum, worst_five_accuracy=subsets[0]['accuracy'],
        per_stock=stocks, five_stock_combinations=subsets, directional_edge_demonstrated=edge,
        empirical_target_met=empirical, simulated_target_met=empirical and evidence_kind == 'synthetic',
        real_world_target_verified=False,
        minimum_stock_accuracy_interval=dict(low=float(np.quantile(minima, .025)),
            high=float(np.quantile(minima, .975)), samples=bootstrap_samples, block_dates=block,
            method='exploratory_circular_market_date_block_bootstrap_of_minimum_stock_accuracy'),
        limitations=['Retrospective model selection and synthetic experiments cannot certify future accuracy.',
                     'An independent, frozen prospective test and verified point-in-time data remain required.',
                     'Blocks concatenate the supplied decision calendar, which may contain gaps.',
                     'Fixed ten are not the entire historical or current 0050 membership.'])


@dataclass(frozen=True)
class DirectionCandidate:
    model: str
    feature_profile: str = 'portable_price'
    window_dates: int | None = None

    def __post_init__(self):
        if self.model not in {'prior', 'logistic', 'hist', 'extra_trees', 'random_forest', 'factor_ridge', 'factor_hist'}:
            raise ValueError('Unknown direction model')
        if self.feature_profile not in {'portable_price', 'portable_market', 'portable_all'}:
            raise ValueError('Unknown direction feature profile')
        if self.window_dates is not None and self.window_dates < 10:
            raise ValueError('Direction training window needs at least ten dates')

    @property
    def name(self):
        return f'{self.model}:{self.feature_profile}:{self.window_dates or "expanding"}'


DEFAULT_CANDIDATES = (
    DirectionCandidate('prior'),
    DirectionCandidate('logistic', window_dates=252),
    DirectionCandidate('logistic', window_dates=756),
    DirectionCandidate('logistic', 'portable_all', 756),
    DirectionCandidate('hist', window_dates=756),
    DirectionCandidate('hist', 'portable_all', 756),
    DirectionCandidate('extra_trees', 'portable_all', 756),
    DirectionCandidate('random_forest', 'portable_all', 756),
)
FACTOR_CANDIDATES = DEFAULT_CANDIDATES + (
    DirectionCandidate('factor_ridge', 'portable_all', 756),
    DirectionCandidate('factor_hist', 'portable_all', 756),
)
THRESHOLDS = (.4, .45, .5, .55, .6)
SELECTION_POLICIES = ('worst_stock_accuracy', 'pooled_brier', 'worst_stock_brier')


@dataclass
class DirectionSearchResult:
    predictions: pd.DataFrame
    report: dict
    transfer_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)


def _calibration_metrics(calibration, probability, threshold, horizon):
    actual = calibration[f'target_{horizon}'].gt(0).to_numpy()
    decision = probability >= threshold
    accuracy = pd.Series(decision == actual, index=calibration.index).groupby(calibration.ticker).mean()
    recalls = [float((decision[actual == y] == y).mean()) for y in (False, True) if (actual == y).any()]
    losses = pd.Series((probability - actual) ** 2, index=calibration.index)
    return dict(minimum_stock_accuracy=float(accuracy.min()), balanced_accuracy=float(np.mean(recalls)),
                brier=float(losses.mean()), worst_stock_brier=float(losses.groupby(calibration.ticker).mean().max()),
                predicted_up_fraction=float(decision.mean()))


def _selection_key(metrics, threshold, policy):
    if policy == 'worst_stock_accuracy':
        return (metrics['minimum_stock_accuracy'], metrics['balanced_accuracy'],
                -metrics['brier'], -abs(threshold - .5))
    if policy == 'pooled_brier':
        return (-metrics['brier'], -metrics['worst_stock_brier'])
    return (-metrics['worst_stock_brier'], -metrics['brier'])


def search_direction_models(frame, config, *, candidates=DEFAULT_CANDIDATES, holdout_dates=126,
                            evidence_kind='unknown', bootstrap_samples=500, output=None,
                            selection_policy='worst_stock_accuracy'):
    """Nested chronological selection of a shared direction model per fold.

    Candidate transforms and classifiers fit training rows only. The subsequent
    purged calibration block chooses the candidate and threshold, before any
    outer test outcomes are scored. Transfer assets never enter selection.
    """
    from threadpoolctl import threadpool_limits
    from .comparison import reserve_development
    from .features import get_feature_columns, select_feature_columns
    from .direction_models import DirectionCandidateUnavailable, fit_direction_model
    from .research import _numeric, _positive_probability, _validate, temporal_splits
    from .workflows import source_digest, write_json
    if config.objective != 'close_return':
        raise ValueError('Direction goal requires the close_return objective')
    if not candidates or len({c.name for c in candidates}) != len(candidates):
        raise ValueError('Direction candidates must have unique names and be nonempty')
    if selection_policy not in SELECTION_POLICIES:
        raise ValueError('Unknown direction selection policy')
    thresholds = THRESHOLDS if selection_policy == 'worst_stock_accuracy' else (.5,)
    development, start = reserve_development(frame, config.horizons, holdout_dates)
    fixed = _validate(development, get_feature_columns(development), config)
    external = (_validate(development, get_feature_columns(development), config, training=False)
                if config.evaluation_tickers else pd.DataFrame())
    protocol = dict(config=asdict(config), candidates=[asdict(c) for c in candidates],
        thresholds=list(thresholds), selection_policy=selection_policy,
        holdout_start=str(start.date()), holdout_dates=holdout_dates,
        holdout_opened=False, evidence_kind=evidence_kind, training_tickers=list(TRAINING_TICKERS),
        source_sha256=source_digest(),
        development_sha256=hashlib.sha256(pd.util.hash_pandas_object(fixed, index=False).to_numpy().tobytes()).hexdigest(),
        selection_rule=('Maximize worst-stock calibration accuracy, then pooled balanced accuracy, then minimize Brier, then prefer threshold nearest 0.5.'
            if selection_policy == 'worst_stock_accuracy' else
            'Minimize pooled calibration Brier, then worst-stock Brier; fixed threshold 0.5.' if selection_policy == 'pooled_brier' else
            'Minimize worst-stock calibration Brier, then pooled Brier; fixed threshold 0.5.') + ' Candidate ties prefer declared order.',
        evaluation='Purged chronological train / candidate-selection calibration / outer test. Final reserved period is excluded.',
        limitation='Exploratory historical development, not an independently untouched or prospective test.')
    if output is not None:
        output = Path(output)
        if (output / 'protocol.json').exists():
            raise ValueError('Use a fresh output directory; experiment records cannot be overwritten')
        write_json(output / 'protocol.json', protocol)
    predictions, selections, audits, transfers = [], [], {}, []
    for horizon in config.horizons:
        horizon_rows, expected_dates = [], []
        for fold, split in enumerate(temporal_splits(fixed, horizon, config), 1):
            train, calibration, test = (fixed.loc[split[k]].copy() for k in ('train', 'calibration', 'test'))
            winner, winner_key, candidate_scores = None, None, []
            # Parallelism is bounded to avoid native numerical oversubscription.
            with threadpool_limits(limits=1):
                for candidate in candidates:
                    columns = select_feature_columns(train, candidate.feature_profile)
                    try:
                        model, subset = fit_direction_model(candidate, train, columns, horizon, config.random_state)
                    except DirectionCandidateUnavailable as exc:
                        candidate_scores.append(dict(candidate=candidate.name, status='unavailable', reason=exc.code))
                        continue
                    probability = _positive_probability(model, _numeric(calibration, columns))
                    for threshold in thresholds:
                        metrics = _calibration_metrics(calibration, probability, threshold, horizon)
                        key = _selection_key(metrics, threshold, selection_policy)
                        candidate_scores.append(dict(candidate=candidate.name, threshold=threshold,
                                                     **metrics, selection_score=list(key)))
                        if winner_key is None or key > winner_key:
                            winner_key = key
                            winner = (candidate, model, columns, threshold, subset)
                if winner is None:
                    raise ValueError(f'No eligible direction candidates for horizon {horizon}, fold {fold}')
                candidate, model, columns, threshold, subset = winner
                predicted = _positive_probability(model, _numeric(test, columns))
            rows = test[['ticker', 'date', 'decision_ts']].copy()
            rows['horizon'], rows['fold'] = horizon, fold
            rows['probability_up'], rows['decision_threshold'] = predicted, threshold
            rows['actual_return'] = np.expm1(test[f'target_{horizon}'])
            rows['training_probability'] = float(subset[f'target_{horizon}'].gt(0).mean())
            rows['candidate'] = candidate.name
            version_settings = json.dumps(dict(candidate=asdict(candidate), horizon=horizon,
                random_state=config.random_state, threshold=threshold, features=columns,
                selection_policy=selection_policy,
                source_sha256=protocol['source_sha256']), sort_keys=True).encode()
            rows['model_version'] = hashlib.sha256(pd.util.hash_pandas_object(
                subset[['ticker', 'date', *columns, f'target_{horizon}']], index=False).to_numpy().tobytes()
                + pd.util.hash_pandas_object(calibration[['ticker', 'date', *columns, f'target_{horizon}']],
                                             index=False).to_numpy().tobytes() + version_settings).hexdigest()[:20]
            if not external.empty:
                transfer = external.loc[external.date.isin(test.date.unique())].copy()
                if not transfer.empty:
                    with threadpool_limits(limits=1):
                        probability = _positive_probability(model, _numeric(transfer, columns))
                    transferred = transfer[['ticker', 'date', 'decision_ts']].copy()
                    transferred['horizon'], transferred['fold'] = horizon, fold
                    transferred['probability_up'], transferred['decision_threshold'] = probability, threshold
                    transferred['actual_return'] = np.expm1(transfer[f'target_{horizon}'])
                    transferred['candidate'], transferred['model_version'] = candidate.name, rows.model_version.iloc[0]
                    transfers.append(transferred)
            horizon_rows.append(rows)
            # Keep benchmark sessions even when the entire stock panel is
            # absent. Reconstructing the denominator from surviving rows would
            # hide a complete acquisition outage.
            calendar = development.loc[development.ticker.eq('0050')
                & development.date.between(test.date.min(), test.date.max()), 'date']
            expected_dates.extend(calendar.unique())
            selections.append(dict(horizon=horizon, fold=fold, candidate=candidate.name, threshold=threshold,
                feature_columns=columns, training_dates=int(subset.date.nunique()),
                model_details=getattr(model, 'training_details', {}),
                train_start=str(subset.date.min().date()), train_label_end_max=str(subset[f'label_end_{horizon}'].max()),
                calibration_start=str(calibration.date.min().date()),
                calibration_label_end_max=str(calibration[f'label_end_{horizon}'].max()),
                test_start=str(test.date.min().date()), test_end=str(test.date.max().date()),
                candidate_scores=candidate_scores))
        combined = pd.concat(horizon_rows, ignore_index=True)
        predictions.append(combined)
        audits[str(horizon)] = audit_five_stock_accuracy(combined, expected_dates=expected_dates,
            evidence_kind=evidence_kind, bootstrap_samples=bootstrap_samples, seed=config.random_state)
    report = dict(protocol=protocol, holdout_opened=False, selections=selections, by_horizon=audits,
        candidate_count=len(candidates),
        total_selection_trials=sum('selection_score' in score for s in selections for score in s['candidate_scores']),
        real_world_target_verified=False)
    transfer_predictions = pd.concat(transfers, ignore_index=True) if transfers else pd.DataFrame()
    report['transfer'] = {}
    if not transfer_predictions.empty:
        for ticker, group in transfer_predictions.groupby('ticker'):
            by_horizon = {}
            for horizon, rows in group.groupby('horizon'):
                valid = rows.actual_return.notna() & np.isfinite(rows.actual_return)
                correct = rows.probability_up.ge(rows.decision_threshold).eq(rows.actual_return.gt(0))
                by_horizon[str(horizon)] = dict(accuracy=float(correct[valid].mean()) if valid.any() else None,
                    observations=int(valid.sum()), coverage=float(valid.mean()))
            report['transfer'][ticker] = dict(scope='same_model_unseen_asset_diagnostic_only',
                tradable=asset_class(ticker) != 'index', by_horizon=by_horizon)
    result = DirectionSearchResult(pd.concat(predictions, ignore_index=True), report, transfer_predictions)
    if output is not None:
        result.predictions.to_parquet(output / 'predictions.parquet', index=False)
        result.transfer_predictions.to_parquet(output / 'transfer_predictions.parquet', index=False)
        write_json(output / 'report.json', report)
    return result


def make_information_scenario(*, sessions=1200, strength=0., seed=42, delay_dates=0, missing_fraction=0.):
    """Generate hypothetical decision-time information and independent future noise.

    No historical prices, vendor responses, or missing publication timestamps
    are invented. Strength is an assumption, not an estimated property of any
    unavailable news, financial, or broker dataset.
    """
    if sessions < 40 or not np.isfinite(strength) or strength < 0 or not 0 <= missing_fraction <= 1:
        raise ValueError('Invalid information scenario settings')
    if not isinstance(delay_dates, int) or delay_dates < 0 or delay_dates >= sessions:
        raise ValueError('Invalid information delay')
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2018-01-01', periods=sessions)
    # Same-date market shocks create realistic cross-stock dependence; these
    # business dates do not claim to reproduce Taiwan's exchange calendar.
    information = .6 * rng.normal(size=(sessions, 1)) + .8 * rng.normal(size=(sessions, 10))
    innovations = .6 * rng.normal(size=(sessions, 1)) + .8 * rng.normal(size=(sessions, 10))
    future_return = .01 * (strength * information + innovations)
    missing = rng.uniform(size=(sessions, 10)) < missing_fraction
    rows = []
    for index, ticker in enumerate((*TRAINING_TICKERS, '0050')):
        signal = pd.Series(information[:, index] if index < 10 else information.mean(axis=1))
        observed = signal.shift(delay_dates)
        if index < 10:
            observed = observed.mask(missing[:, index])
        target = future_return[:, index] if index < 10 else future_return.mean(axis=1)
        frame = pd.DataFrame(dict(ticker=ticker, date=dates,
            decision_ts=(dates + pd.Timedelta(hours=20)).tz_localize('Asia/Taipei').tz_convert('UTC'),
            f_hypothetical_information=observed.to_numpy(), target_1=target,
            label_end_1=pd.Series(dates).shift(-1).to_numpy(),
            benchmark_target_1=future_return.mean(axis=1), source='synthetic_hypothetical_information'))
        frame.loc[frame.label_end_1.isna(), ['target_1', 'benchmark_target_1']] = np.nan
        frame['label_available_at_1'] = (frame.label_end_1.dt.tz_localize('Asia/Taipei')
                                        + pd.Timedelta(hours=20)).dt.tz_convert('UTC')
        rows.append(frame)
    metadata = dict(evidence_kind='synthetic', provider_data_obtained=False, strength=strength, seed=seed,
        sessions=sessions, delay_dates=delay_dates, missing_fraction=missing_fraction,
        process='Next-session log return = 0.01 * (strength * latent information known at decision + independent Gaussian future innovation). Same-date stock correlation is 0.36.',
        idealized_sign_accuracy=float(.5 + np.arctan(strength) / np.pi),
        idealized_assumptions='Perfect current information, zero missingness and zero delay; Gaussian population expectation, not achieved model accuracy.',
        limitation='Hypothetical information content only. No evidence that an obtainable provider feature has this signal strength. Synthetic business dates are not verified exchange sessions.')
    return pd.concat(rows, ignore_index=True), metadata


def simulate_information_scenarios(*, sessions=1200, seeds=(17, 29, 43), output=None):
    """Run predeclared information assumptions across every requested seed."""
    from .research import ResearchConfig
    from .workflows import write_json
    if sessions < 380 or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError('Need at least 380 sessions and unique, nonempty seeds')
    scenarios = dict(no_information=dict(strength=0.), weak=dict(strength=.5),
        informative=dict(strength=3.), delayed=dict(strength=3., delay_dates=1),
        half_missing=dict(strength=3., missing_fraction=.5))
    protocol = dict(evidence_kind='synthetic', sessions=sessions, seeds=list(seeds), scenarios=scenarios,
        provider_data_obtained=False, rule='Report every predeclared scenario and seed; never select a winning random seed.',
        real_world_target_verified=False)
    if output is not None:
        output = Path(output)
        if (output / 'protocol.json').exists():
            raise ValueError('Use a fresh simulation output directory')
        write_json(output / 'protocol.json', protocol)
    experiments, aggregates = [], {}
    for name, assumptions in scenarios.items():
        metrics = []
        for seed in seeds:
            frame, hypothesis = make_information_scenario(sessions=sessions, seed=seed, **assumptions)
            config = ResearchConfig(horizons=(1,), min_train_dates=126, calibration_dates=42,
                                    test_dates=63, max_folds=8, random_state=seed)
            result = search_direction_models(frame, config, holdout_dates=30, evidence_kind='synthetic',
                candidates=(DirectionCandidate('logistic'), DirectionCandidate('prior')),
                bootstrap_samples=100, output=output / name / str(seed) if output else None)
            audit = result.report['by_horizon']['1']
            metrics.append(audit)
            experiments.append(dict(scenario=name, seed=seed, hypothesis=hypothesis,
                minimum_stock_accuracy=audit['minimum_stock_accuracy'], overall_accuracy=audit['overall_accuracy'],
                worst_five_accuracy=audit['worst_five_accuracy'], coverage=audit['coverage'],
                simulated_target_met=audit['simulated_target_met'], real_world_target_verified=False))
        aggregates[name] = dict(seeds_tested=list(seeds),
            minimum_stock_accuracy_across_seeds=min(m['minimum_stock_accuracy'] for m in metrics),
            average_overall_accuracy=float(np.mean([m['overall_accuracy'] for m in metrics])),
            all_seeds_simulated_target_met=all(m['simulated_target_met'] for m in metrics))
    report = {**protocol, 'experiments': experiments, 'scenarios': aggregates, 'assumptions': scenarios}
    if output is not None:
        write_json(output / 'report.json', report)
    return report
