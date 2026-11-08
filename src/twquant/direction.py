"""Direction research with a fixed universe, full coverage and explicit evidence labels.

This module does not promote a model, open a final holdout, or issue forecasts.
Zero returns belong to the non-up class, as in the existing research engine.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .universe import TRAINING_TICKERS, asset_class
from .direction_audit import audit_constituent_accuracy, audit_five_stock_accuracy, constituent_universe


@dataclass(frozen=True)
class DirectionCandidate:
    model: str
    feature_profile: str = 'portable_price'
    window_dates: int | None = None

    def __post_init__(self):
        if self.model not in {'prior', 'logistic', 'hist', 'extra_trees', 'random_forest', 'factor_ridge', 'factor_hist',
                              'factor_beta_ridge', 'factor_beta_hist', 'factor_beta_vol_ridge', 'factor_beta_vol_hist'}:
            raise ValueError('Unknown direction model')
        if self.feature_profile not in {'portable_price', 'portable_market', 'portable_all', 'portable_path', 'portable_sector'}:
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
MARKET_CANDIDATES = FACTOR_CANDIDATES + tuple(
    DirectionCandidate(model, 'portable_market', 756)
    for model in ('logistic', 'hist', 'extra_trees', 'random_forest', 'factor_ridge', 'factor_hist')
)
EXPOSURE_CANDIDATES = MARKET_CANDIDATES + tuple(
    DirectionCandidate(model, profile, 756)
    for model in ('factor_beta_ridge', 'factor_beta_hist', 'factor_beta_vol_ridge', 'factor_beta_vol_hist')
    for profile in ('portable_all', 'portable_market')
)
PATH_CANDIDATES = EXPOSURE_CANDIDATES + tuple(
    DirectionCandidate(model, 'portable_path', 756)
    for model in ('logistic', 'hist', 'extra_trees', 'random_forest', 'factor_beta_ridge', 'factor_beta_vol_ridge')
)
SECTOR_CANDIDATES = PATH_CANDIDATES + tuple(
    DirectionCandidate(model, 'portable_sector', 756)
    for model in ('logistic', 'hist', 'extra_trees', 'random_forest', 'factor_beta_ridge', 'factor_beta_vol_ridge')
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
                            selection_policy='worst_stock_accuracy', constituents=None, membership_as_of=None,
                            mode='postclose'):
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
    if mode not in {'postclose', 'preopen'}:
        raise ValueError('Unknown direction decision mode')
    primary = frame.loc[frame.ticker.astype(str).isin((*TRAINING_TICKERS, '0050'))]
    local = pd.to_datetime(primary.decision_ts, utc=True).dt.tz_convert('Asia/Taipei')
    anchor = pd.to_datetime(primary.date).dt.normalize()
    local_date = local.dt.tz_localize(None).dt.normalize()
    hour, minute = (20, 0) if mode == 'postclose' else (8, 30)
    clock = local.dt.hour.eq(hour) & local.dt.minute.eq(minute) & local.dt.second.eq(0)
    date_valid = local_date.eq(anchor) if mode == 'postclose' else local_date.gt(anchor)
    if not (clock & date_valid).loc[local.notna()].all():
        raise ValueError('Direction mode does not match the primary decision-time contract')
    if constituents is not None:
        constituents = constituent_universe(constituents)
        missing = set(constituents) - set(TRAINING_TICKERS) - set(config.evaluation_tickers)
        if missing:
            raise ValueError('Configure every external constituent as evaluation-only before research')
        date = pd.Timestamp(membership_as_of) if membership_as_of is not None else pd.NaT
        if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
            raise ValueError('Constituent research requires a daily membership snapshot date')
        membership_as_of = date.date().isoformat()
    thresholds = THRESHOLDS if selection_policy == 'worst_stock_accuracy' else (.5,)
    development, start = reserve_development(frame, config.horizons, holdout_dates)
    fixed = _validate(development, get_feature_columns(development), config)
    external = (_validate(development, get_feature_columns(development), config, training=False)
                if config.evaluation_tickers else pd.DataFrame())
    # A stock direction task does not require an ETF quote at the future
    # horizon. Use the observed primary-market calendar, never transfer assets,
    # and keep the final reserved outcomes excluded by calendar maturity.
    primary_dates = pd.DatetimeIndex(development.loc[
        development.ticker.isin((*TRAINING_TICKERS, '0050')), 'date'].unique()).sort_values()
    split_frame = fixed.drop(columns=[f'benchmark_target_{h}' for h in config.horizons], errors='ignore').copy()
    for h in config.horizons:
        split_frame.loc[split_frame[f'label_end_{h}'].ge(start), f'label_end_{h}'] = pd.NaT
    protocol = dict(config=asdict(config), candidates=[asdict(c) for c in candidates],
        us_sector_features=sorted(c for c in get_feature_columns(fixed) if c.startswith('f_ext_us_sector_')),
        price_path_features=sorted(c for c in get_feature_columns(fixed) if c.startswith('f_path_')),
        taifex_night_features=sorted(c for c in get_feature_columns(fixed) if c.startswith('f_ext_taifex_')),
        taifex_exposure_features=sorted(c for c in get_feature_columns(fixed)
            if c.startswith('f_ext_taifex_') and c.endswith(('_beta60', '_beta_night_return'))),
        overseas_window_features=sorted(c for c in get_feature_columns(fixed)
            if c.startswith('f_ext_fmp_') and c.endswith(('_window_return', '_window_updated'))),
        mode=mode, decision_contract='20:00 on anchor session' if mode == 'postclose' else '08:30 on next observed market session; price anchor remains previous close',
        thresholds=list(thresholds), selection_policy=selection_policy,
        holdout_start=str(start.date()), holdout_dates=holdout_dates,
        holdout_opened=False, evidence_kind=evidence_kind, training_tickers=list(TRAINING_TICKERS),
        calendar_policy='observed_fixed_ten_and_0050_sessions; benchmark_future_quotes_do_not_gate_stock_predictions',
        source_sha256=source_digest(),
        development_sha256=hashlib.sha256(pd.util.hash_pandas_object(fixed, index=False).to_numpy().tobytes()).hexdigest(),
        selection_rule=('Maximize worst-stock calibration accuracy, then pooled balanced accuracy, then minimize Brier, then prefer threshold nearest 0.5.'
            if selection_policy == 'worst_stock_accuracy' else
            'Minimize pooled calibration Brier, then worst-stock Brier; fixed threshold 0.5.' if selection_policy == 'pooled_brier' else
            'Minimize worst-stock calibration Brier, then pooled Brier; fixed threshold 0.5.') + ' Candidate ties prefer declared order.',
        evaluation='Purged chronological train / candidate-selection calibration / outer test. Final reserved period is excluded.',
        limitation='Exploratory historical development, not an independently untouched or prospective test.')
    if constituents is not None:
        protocol['constituent_audit'] = dict(tickers=list(constituents), membership_as_of=membership_as_of,
            selection_scope='fixed_ten_only', historical_membership_verified=False)
    if output is not None:
        output = Path(output)
        if (output / 'protocol.json').exists():
            raise ValueError('Use a fresh output directory; experiment records cannot be overwritten')
        write_json(output / 'protocol.json', protocol)
    predictions, selections, audits, transfers, expected_calendars = [], [], {}, [], {}
    for horizon in config.horizons:
        horizon_rows, expected_dates = [], []
        for fold, split in enumerate(temporal_splits(split_frame, horizon, config, calendar_dates=primary_dates), 1):
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
                predicted = _positive_probability(model, _numeric(test, columns)) if len(test) else np.empty(0)
            rows = test[['ticker', 'date', 'decision_ts']].copy()
            rows['horizon'], rows['fold'] = horizon, fold
            rows['decision_mode'] = mode
            rows['probability_up'], rows['decision_threshold'] = predicted, threshold
            rows['actual_return'] = np.expm1(test[f'target_{horizon}'])
            rows['training_probability'] = float(subset[f'target_{horizon}'].gt(0).mean())
            rows['candidate'] = candidate.name
            version_settings = json.dumps(dict(candidate=asdict(candidate), horizon=horizon,
                random_state=config.random_state, threshold=threshold, features=columns,
                selection_policy=selection_policy,
                mode=mode,
                source_sha256=protocol['source_sha256']), sort_keys=True).encode()
            model_version = hashlib.sha256(pd.util.hash_pandas_object(
                subset[['ticker', 'date', *columns, f'target_{horizon}']], index=False).to_numpy().tobytes()
                + pd.util.hash_pandas_object(calibration[['ticker', 'date', *columns, f'target_{horizon}']],
                                             index=False).to_numpy().tobytes() + version_settings).hexdigest()[:20]
            rows['model_version'] = model_version
            if not external.empty:
                transfer = external.loc[external.date.isin(split['test_calendar'])].copy()
                if not transfer.empty:
                    with threadpool_limits(limits=1):
                        probability = _positive_probability(model, _numeric(transfer, columns))
                    transferred = transfer[['ticker', 'date', 'decision_ts']].copy()
                    transferred['horizon'], transferred['fold'] = horizon, fold
                    transferred['decision_mode'] = mode
                    transferred['probability_up'], transferred['decision_threshold'] = probability, threshold
                    transferred['actual_return'] = np.expm1(transfer[f'target_{horizon}'])
                    transferred['candidate'], transferred['model_version'] = candidate.name, model_version
                    transfers.append(transferred)
            if len(rows):
                horizon_rows.append(rows)
            expected_dates.extend(split['test_calendar'])
            selections.append(dict(horizon=horizon, fold=fold, candidate=candidate.name, threshold=threshold,
                feature_columns=columns, training_dates=int(subset.date.nunique()),
                model_details=getattr(model, 'training_details', {}),
                train_start=str(subset.date.min().date()), train_label_end_max=str(subset[f'label_end_{horizon}'].max()),
                calibration_start=str(calibration.date.min().date()),
                calibration_label_end_max=str(calibration[f'label_end_{horizon}'].max()),
                test_start=str(split['test_calendar'][0].date()), test_end=str(split['test_calendar'][-1].date()),
                candidate_scores=candidate_scores))
        if not horizon_rows:
            raise ValueError('No observed fixed-ten test predictions on the declared market calendar')
        combined = pd.concat(horizon_rows, ignore_index=True)
        predictions.append(combined)
        expected_calendars[str(horizon)] = expected_dates
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
    constituent_predictions = None
    if constituents is not None:
        panel = pd.concat([result.predictions, transfer_predictions], ignore_index=True)
        constituent_predictions = panel.loc[panel.ticker.isin(constituents)].copy()
        report['constituents'] = dict(membership_as_of=membership_as_of, evaluation_tickers=list(constituents),
            selection_scope='fixed_ten_only', historical_membership_verified=False, real_world_target_verified=False,
            by_horizon={str(h): audit_constituent_accuracy(
                constituent_predictions.loc[constituent_predictions.horizon.eq(h)], constituents,
                membership_as_of=membership_as_of, evidence_kind=evidence_kind,
                expected_dates=expected_calendars[str(h)], bootstrap_samples=bootstrap_samples, seed=config.random_state)
                for h in config.horizons})
    if output is not None:
        result.predictions.to_parquet(output / 'predictions.parquet', index=False)
        result.transfer_predictions.to_parquet(output / 'transfer_predictions.parquet', index=False)
        if constituent_predictions is not None:
            constituent_predictions.to_parquet(output / 'constituent_predictions.parquet', index=False)
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
