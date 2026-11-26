"""One fixed preopen task, with timestamped inputs and full-panel scoring."""
from dataclasses import dataclass
import hashlib
import json

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .direction_audit import audit_constituent_accuracy, audit_five_stock_accuracy, constituent_universe
from .features import FEATURE_VERSION, _timestamps
from .ledger import json_safe
from .universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS

TASK = '0859_open_above_previous_session_close'
LOCAL = 'Asia/Taipei'
DECISION = pd.Timedelta(hours=8, minutes=59)
BASE_SNAPSHOT = pd.Timedelta(hours=8, minutes=30)
TARGET = .75
MIN_OPTIONAL_DATES = 63
REQUIRED_COLUMNS = ('date', 'ticker', 'decision_ts', 'available_at', 'open', 'close', 'volume')

PRICE = ('f_return_1', 'f_return_5', 'f_return_20', 'f_return_60', 'f_gap', 'f_intraday',
         'f_range', 'f_close_location', 'f_volatility_20', 'f_volatility_60', 'f_price_ma20',
         'f_rsi_14', 'f_market_return_1', 'f_market_return_5', 'f_peer_return_ex_target', 'f_beta_60')
VOLUME = ('f_rvol_20', 'f_amihud_20')
US = tuple(f'f_ext_{source}_{symbol}_window_return' for source, symbols in (
    ('fmp', ('spy', 'tsm', 'nvda')), ('us_sector', ('smh', 'xlf', 'xlp', 'xlb', 'xle', 'xlc', 'xli')))
    for symbol in symbols)
NIGHT = tuple(f'f_ext_taifex_{contract}_night_{measure}'
              for contract in ('tx', 'te', 'tf') for measure in ('return', 'range', 'volume_ratio'))
OTHER = ('f_flow_foreign_imbalance', 'f_flow_foreign_mean5', 'f_flow_trust_imbalance',
         'f_margin_finance_utilization', 'f_margin_short_utilization', 'f_ext_fred_VIXCLS',
         'f_ext_fred_DEXTAUS', 'f_ext_fred_DGS10')
BASE_GROUPS = dict(price=PRICE, volume=VOLUME, us=US, night=NIGHT, other=OTHER)
EVENT_FIELDS = {
    'auction_indicative_price': 'f_open_auction_gap',
    'auction_imbalance': 'f_open_auction_imbalance',
    'auction_matched_volume_ratio': 'f_open_auction_rvol',
    'official_event_count': 'f_open_official_count',
    'media_event_count': 'f_open_media_count',
    'media_sentiment': 'f_open_media_sentiment',
    'tx_morning_return': 'f_open_tx_morning',
    'usdtwd_morning_return': 'f_open_usdtwd_morning',
    'regional_equity_return': 'f_open_regional_equity_return',
    'company_action_gap': 'f_open_company_action_gap',
    'earnings_surprise': 'f_open_earnings_surprise',
    'guidance_revision': 'f_open_guidance_revision',
}
AUCTION = tuple(value for key, value in EVENT_FIELDS.items() if key.startswith('auction_'))
NEWS = tuple(EVENT_FIELDS[key] for key in ('official_event_count', 'media_event_count', 'media_sentiment'))
MORNING = tuple(EVENT_FIELDS[key] for key in ('tx_morning_return', 'usdtwd_morning_return', 'regional_equity_return'))
COMPANY = tuple(EVENT_FIELDS[key] for key in ('company_action_gap', 'earnings_surprise', 'guidance_revision'))
OPTIONAL_GROUPS = dict(auction=AUCTION, news=NEWS, morning=MORNING, company=COMPANY)
GROUPS = {**BASE_GROUPS, **OPTIONAL_GROUPS}
OVERNIGHT_SIGNAL = (NIGHT[0], US[0], US[1])


def _sessions(values):
    dates = pd.DatetimeIndex(pd.to_datetime(values))
    if len(dates) < 2 or dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError('Provide at least two naive daily exchange session dates')
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('Exchange sessions must be strictly increasing')
    return dates


def _finite(values):
    return pd.to_numeric(values, errors='coerce').replace([np.inf, -np.inf], np.nan)


def _add_events(panel, events):
    for column in EVENT_FIELDS.values():
        panel[column] = np.nan
    if events is None or events.empty:
        return
    required = {'session_date', 'ticker', 'feature', 'value', 'published_at', 'available_at'}
    if not required <= set(events):
        raise ValueError('Opening events require session_date, ticker, feature, value, published_at and available_at')
    values = events[list(required)].copy()
    dates = pd.DatetimeIndex(pd.to_datetime(values.session_date))
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError('Opening events require naive daily session dates')
    values['date'] = dates
    values['ticker'] = values.ticker.astype(str)
    if not values.feature.isin(EVENT_FIELDS).all():
        raise ValueError('Unsupported opening event feature')
    values['column'] = values.feature.map(EVENT_FIELDS)
    global_rows = values.ticker.eq('*')
    if (global_rows & ~values.column.isin(MORNING)).any():
        raise ValueError('Only morning market series may use the global ticker')
    values['value'] = _finite(values.value)
    values['published_at'] = _timestamps(values.published_at, 'published_at')
    values['available_at'] = _timestamps(values.available_at, 'available_at')
    if values.value.isna().any() or values.published_at.gt(values.available_at).any():
        raise ValueError('Opening events must have finite values and ordered clocks')
    if values.duplicated(['date', 'ticker', 'feature', 'available_at']).any():
        raise ValueError('Ambiguous same-time opening event versions')
    session = values.date.dt.tz_localize(LOCAL).dt.tz_convert('UTC')
    cutoff = session + DECISION
    t0 = values.published_at
    auction = values.column.isin(AUCTION)
    morning = values.column.isin(MORNING)
    earliest = cutoff - pd.Timedelta(seconds=30)
    morning_start = session + pd.Timedelta(hours=8, minutes=45)
    valid = values.available_at.lt(cutoff)
    valid &= (~auction | t0.ge(earliest)) & (~morning | t0.ge(morning_start))
    for (column, market_wide), group in values.loc[valid].groupby(['column', global_rows[valid]]):
        keys = ['date'] if market_wide else ['date', 'ticker']
        latest = group.sort_values('available_at').drop_duplicates(keys, keep='last')
        available = panel[keys].merge(latest[keys + ['value']], on=keys, how='left').value
        panel[column] = available.combine_first(panel[column]) if not market_wide else panel[column].combine_first(available)
    price = panel['f_open_auction_gap']
    panel['f_open_auction_gap'] = price.where(price.gt(0)) / panel.reference_close - 1


def build_opening_gap_panel(frame, calendar, symbols, *, events=None):
    """Align 08:30 history and optional receipt-timed updates to each 08:59 decision."""
    days = _sessions(calendar)
    if not set(REQUIRED_COLUMNS) <= set(frame):
        raise ValueError('Opening study requires a preopen feature snapshot with raw daily bars')
    symbols = tuple(symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError('Duplicate opening-universe symbols')
    bars = frame.loc[frame.ticker.isin(symbols)].copy()
    dates = pd.DatetimeIndex(pd.to_datetime(bars.date))
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError('Opening snapshot dates must be naive trading dates')
    bars['date'] = dates
    bars = bars.loc[bars.date.isin(days)].copy()
    for clock in ('available_at', 'decision_ts'):
        bars[clock] = _timestamps(bars[clock], clock, allow_missing=True)
    if bars.duplicated(['date', 'ticker']).any():
        raise ValueError('Duplicate ticker and date in opening-price snapshot')
    # Compare raw prices so that neither an ex-post adjustment nor a future close enters the task.
    grid = pd.MultiIndex.from_product([days, symbols], names=['date', 'ticker']).to_frame(index=False)
    grid = grid.merge(bars, on=['date', 'ticker'], how='left', validate='one_to_one')
    prior = grid.groupby('ticker', sort=False).shift()
    panel = grid[['date', 'ticker']].copy()
    local = panel.date.dt.tz_localize(LOCAL).dt.tz_convert('UTC')
    panel['decision_ts'] = local + DECISION
    bar_mature = prior.available_at.lt(panel.decision_ts)
    snapshot_ok = prior.decision_ts.eq(local + BASE_SNAPSHOT)
    if (prior.decision_ts.notna() & ~snapshot_ok).any():
        raise ValueError('Input frame must have prior-session features aligned to the next exchange day at 08:30')
    prior_close = _finite(prior.close)
    prior_traded = prior.volume.gt(0) | panel.ticker.eq('IX0001')
    panel['reference_close'] = prior_close.where(bar_mature & prior_traded & prior_close.gt(0))
    open_price = _finite(grid.open)
    current_traded = grid.volume.gt(0) | panel.ticker.eq('IX0001')
    answer_ready = grid.available_at.gt(panel.decision_ts)
    actual = open_price.where(current_traded & answer_ready & open_price.gt(0)) / panel.reference_close - 1
    panel['actual_return'] = actual
    panel['label_available_at'] = grid.available_at.where(actual.notna())
    for column in (*PRICE, *VOLUME, *US, *NIGHT, *OTHER):
        if column not in prior:
            panel[column] = np.nan
            continue
        eligible = snapshot_ok
        if column in (*PRICE, *VOLUME, *OTHER[:5]):
            eligible = eligible & bar_mature
        if column in NIGHT:
            age = prior.get(f'{column}_age_days')
            eligible = eligible & (age.between(0, .25) if age is not None else False)
        panel[column] = _finite(prior[column]).where(eligible)
    _add_events(panel, events)
    return panel.loc[panel.date.gt(days[0])].reset_index(drop=True)


@dataclass
class OpeningGapModel:
    name: str
    prior: float
    columns: tuple[str, ...]
    estimator: object = None

    def predict(self, panel):
        """Return one probability per input row in its original order."""
        if not self.columns:
            return np.full(len(panel), self.prior)
        features = panel.reindex(columns=self.columns)
        if self.estimator is None:
            signal = features.bfill(axis=1).iloc[:, 0]
            return np.where(signal.isna(), self.prior, np.where(signal.gt(0), .75, .25))
        p = self.estimator.predict_proba(features)[:, 1]
        return np.where(features.notna().any(axis=1), p, self.prior)

    def explain(self, panel):
        """Attribute a probability to the source groups in their declared order."""
        observations = panel.reindex(columns=self.columns)
        visible = pd.DataFrame(np.nan, index=panel.index, columns=self.columns)
        previous = np.full(len(panel), self.prior)
        contributions = {'factor_base': previous}
        for group, members in GROUPS.items():
            columns = [name for name in members if name in self.columns]
            if not columns:
                continue
            visible[columns] = observations[columns]
            current = self.predict(visible)
            contributions[f'impact_{group}'] = current - previous
            previous = current
        if not np.allclose(previous, self.predict(panel), atol=1e-12, rtol=0):
            raise ValueError('Opening factor groups must account for every model input')
        return pd.concat([observations, pd.DataFrame(contributions, index=panel.index)], axis=1)


def _linear_estimator():
    return make_pipeline(SimpleImputer(strategy='median', add_indicator=True), StandardScaler(),
                         LogisticRegression(C=.1, max_iter=500))


@dataclass
class SourceFusionEstimator:
    experts: tuple
    stacker: object

    def _inputs(self, panel):
        probabilities, coverage = [], []
        for _, columns, model, prior in self.experts:
            frame = panel.loc[:, list(columns)]
            observed = frame.notna().any(axis=1).to_numpy()
            probability = model.predict_proba(frame)[:, 1]
            probabilities.append(np.where(observed, probability, prior))
            coverage.append(observed)
        return np.column_stack((*probabilities, *coverage))

    def predict_proba(self, panel):
        return self.stacker.predict_proba(self._inputs(panel))

    def source_weights(self):
        count = len(self.experts)
        return {group: dict(probability=float(self.stacker.coef_[0, index]),
                            present=float(self.stacker.coef_[0, index + count]))
                for index, (group, *_) in enumerate(self.experts)}


def _fit_source_fusion(train, prior, optional):
    dates = pd.Index(train.date.unique()).sort_values()
    if len(dates) < 4:
        return None
    split = dates[int(.75 * len(dates))]
    later = train.loc[train.date.ge(split)]
    first_decision = later.decision_ts.min()
    earlier = train.loc[train.date.lt(split) & train.label_available_at.lt(first_decision)]
    early_y = earlier.actual_return.gt(0).astype(int)
    late_y = later.actual_return.gt(0).astype(int)
    if early_y.nunique() < 2 or late_y.nunique() < 2:
        return None
    experts = []
    for group, names in GROUPS.items():
        columns = tuple(c for c in names if c in earlier and earlier[c].notna().any() and later[c].notna().any()
                        and (group not in OPTIONAL_GROUPS or
                             (c in optional and earlier.loc[earlier[c].notna(), 'date'].nunique() >= MIN_OPTIONAL_DATES)))
        if columns:
            learner = _linear_estimator().fit(earlier[list(columns)], early_y)
            experts.append((group, columns, learner, float(early_y.mean())))
    if len(experts) < 2:
        return None
    estimator = SourceFusionEstimator(tuple(experts), LogisticRegression(C=1., max_iter=500))
    columns = tuple(key for _, members, *_ in experts for key in members)
    estimator.stacker.fit(estimator._inputs(later[list(columns)]), late_y)
    return OpeningGapModel('source_fusion', prior, columns, estimator)


def _candidate_models(train, *, source_fusion=False):
    y = train.actual_return.gt(0).astype(int)
    prior = float(y.mean())
    models = {'prior': OpeningGapModel('prior', prior, ())}
    available = set(train.columns[train.notna().any()])
    optional = {c for cols in OPTIONAL_GROUPS.values() for c in cols
                if train.loc[train[c].notna(), 'date'].nunique() >= MIN_OPTIONAL_DATES}
    for name, columns in [('overnight_sign', OVERNIGHT_SIGNAL), ('auction_sign', (AUCTION[0],))]:
        usable = tuple(c for c in columns if c in available and (c not in AUCTION or c in optional))
        if usable:
            models[name] = OpeningGapModel(name, prior, usable)
    if y.nunique() < 2:
        return models
    price_volume = PRICE + VOLUME
    overnight = price_volume + US + NIGHT
    context = overnight + OTHER + tuple(c for c in EVENT_FIELDS.values() if c in optional)
    linear = dict(price_volume=price_volume, overnight=overnight, context=context)
    linear.update({f'without_{group}': tuple(c for c in context if c not in columns)
                   for group, columns in GROUPS.items() if group != 'price' and set(context) & set(columns) & available})
    for name, columns in {**linear, 'context_tree': context}.items():
        features = tuple(c for c in columns if c in available)
        if not features:
            continue
        if name == 'context_tree':
            estimator = HistGradientBoostingClassifier(max_iter=125, max_leaf_nodes=15, min_samples_leaf=100,
                                                        l2_regularization=3, early_stopping=False, random_state=42)
        else:
            estimator = _linear_estimator()
        estimator.fit(train[list(features)], y)
        models[name] = OpeningGapModel(name, prior, features, estimator)
    if source_fusion:
        fusion = _fit_source_fusion(train, prior, optional)
        if fusion is not None:
            models[fusion.name] = fusion
    return models


def _predictions(model, panel, version):
    p = panel[['date', 'ticker', 'decision_ts', 'actual_return', 'label_available_at']].copy()
    p['probability_up'] = model.predict(panel)
    p['decision_threshold'] = .5
    p['horizon'] = 1
    p['model_version'] = version
    return p


def _metrics(predictions, symbols):
    up = predictions.actual_return.gt(0)
    valid = predictions.probability_up.between(0, 1)
    known = predictions.actual_return.notna()
    correct = known & valid & up.eq(predictions.probability_up.ge(.5))
    scores = correct.groupby(predictions.ticker).mean().reindex(symbols).sort_values()
    brier = (predictions.loc[known & valid, 'probability_up'] - up[known & valid]) ** 2
    return dict(expected=len(predictions), known=int(known.sum()), answered=int(valid.sum()),
                accuracy=float(correct.mean()), fraction_flat=float((known & predictions.actual_return.eq(0)).mean()),
                always_up_accuracy=float((known & up).mean()), always_not_up_accuracy=float((known & ~up).mean()),
                minimum_stock_accuracy=float(scores.iloc[0]), worst_five_accuracy=float(scores.iloc[:5].mean()),
                brier=float(brier.mean()) if len(brier) else None)


def _scope(predictions, constituents, membership_as_of, evidence_kind):
    dates = predictions.date.unique()
    core = predictions.loc[predictions.ticker.isin(TRAINING_TICKERS)]
    stocks = predictions.loc[predictions.ticker.isin(constituents)]
    return dict(
        fixed_ten=audit_five_stock_accuracy(core, evidence_kind=evidence_kind, expected_dates=dates),
        constituents=audit_constituent_accuracy(stocks, constituents, membership_as_of=membership_as_of,
                                                evidence_kind=evidence_kind, expected_dates=dates),
        transfers={ticker: {**_metrics(predictions.loc[predictions.ticker.eq(ticker)], [ticker]),
                            'tradable': ticker != 'IX0001'}
                   for ticker in DEFAULT_EVALUATION_TICKERS})


@dataclass
class OpeningGapResult:
    report: dict
    predictions: pd.DataFrame
    models: dict


def validate_opening_gap(panel, constituents, *, membership_as_of, evidence_kind='unknown', fold_dates=252,
                         calibration_dates=126, final_dates=126, min_train_dates=504, max_folds=8,
                         source_fusion=False):
    """Fit and select on the fixed ten, then independently score each complete test block."""
    constituents = constituent_universe(constituents)
    if evidence_kind not in {'historical', 'synthetic', 'unknown'}:
        raise ValueError('Unknown opening research evidence kind')
    if min(fold_dates, calibration_dates, final_dates, min_train_dates, max_folds) < 1:
        raise ValueError('Opening validation date windows must be positive')
    dates = _sessions(sorted(panel.date.unique()))
    expected = set((*TRAINING_TICKERS, *constituents, *DEFAULT_EVALUATION_TICKERS))
    if set(panel.ticker) != expected or panel.duplicated(['date', 'ticker']).any() or len(panel) != len(dates) * len(expected):
        raise ValueError('Opening research needs the complete date-by-symbol grid')
    final_start = len(dates) - final_dates
    first = min_train_dates + calibration_dates
    if final_start <= first:
        raise ValueError('Insufficient sessions for an opening development fold and final check')
    starts = list(range(first, final_start, fold_dates))[-max_folds:]
    blocks = [(start, min(start + fold_dates, final_start), 'development') for start in starts]
    blocks.append((final_start, len(dates), 'final'))
    core = panel.loc[panel.ticker.isin(TRAINING_TICKERS)]
    folds, scored, models = [], [], {}
    with threadpool_limits(limits=1):
        for start, stop, phase in blocks:
            cal_dates = dates[start - calibration_dates:start]
            test_dates = dates[start:stop]
            cal_start = cal_dates[0].tz_localize(LOCAL).tz_convert('UTC') + DECISION
            test_start = test_dates[0].tz_localize(LOCAL).tz_convert('UTC') + DECISION
            train = core.loc[core.date.lt(cal_dates[0]) & core.label_available_at.lt(cal_start)
                             & core.actual_return.notna()]
            if train.date.nunique() < min_train_dates:
                raise ValueError('Insufficient mature fixed-ten training dates')
            cal = core.loc[core.date.isin(cal_dates)].copy()
            cal['actual_return'] = cal.actual_return.where(cal.label_available_at.lt(test_start))
            if not cal.actual_return.notna().any():
                raise ValueError('No mature fixed-ten calibration answers')
            test = panel.loc[panel.date.isin(test_dates)]
            candidates = _candidate_models(train, source_fusion=source_fusion)
            calibration = {name: _metrics(_predictions(model, cal, name), TRAINING_TICKERS)
                           for name, model in candidates.items() if not name.startswith('without_')}
            chosen = max(calibration, key=lambda name: (calibration[name]['worst_five_accuracy'],
                         calibration[name]['accuracy'], -calibration[name]['brier']))
            version = f'{TASK}:{test_dates[0].date()}:{chosen}'
            selected = _predictions(candidates[chosen], test, version)
            selected = pd.concat([selected, candidates[chosen].explain(test)], axis=1)
            selected['phase'] = phase
            for name, model in candidates.items():
                selected[f'control_{name}'] = model.predict(test)
            scored.append(selected)
            folds.append(dict(phase=phase, model=chosen, model_version=version,
                              train_rows=len(train), train_dates=int(train.date.nunique()),
                              train_label_available_max=train.label_available_at.max().isoformat(),
                              calibration_decision_start=cal_start.isoformat(),
                              calibration_label_available_max=cal.loc[cal.actual_return.notna(), 'label_available_at'].max().isoformat(),
                              test_decision_start=test_start.isoformat(), test_end=test_dates[-1].date().isoformat(),
                              test_dates=len(test_dates), calibration=calibration,
                              candidate_features={name: model.columns for name, model in candidates.items()}))
            if 'source_fusion' in candidates:
                folds[-1]['source_fusion_weights'] = candidates['source_fusion'].estimator.source_weights()
            models[version] = candidates[chosen]
    predictions = pd.concat(scored, ignore_index=True)
    report = dict(task=TASK, cutoff_local='08:59:00 Asia/Taipei', target_accuracy=TARGET,
                  study_kind='retrospective_opening_gap_research', evidence_kind=evidence_kind,
                  real_world_target_verified=False,
                  folds=folds, source_coverage={
                      scope: {name: float(rows[list(columns)].notna().any(axis=1).mean())
                              for name, columns in GROUPS.items()}
                      for scope, rows in [('fixed_ten', core), ('constituents', panel.loc[panel.ticker.isin(constituents)])]})
    for phase, part in predictions.groupby('phase'):
        summary = _scope(part, constituents, membership_as_of, evidence_kind)
        summary['complete_denominator'] = _metrics(part.loc[part.ticker.isin(constituents)], constituents)
        # Report every policy over the same calendar. Missing candidate probabilities here
        # are charged as misses and expose where a candidate had no trained inputs.
        summary['controls'] = {}
        for col in part.filter(regex='^control_'):
            if part[col].notna().any():
                control = part.assign(probability_up=part[col])
                summary['controls'][col.removeprefix('control_')] = {
                    'constituents': _metrics(control.loc[control.ticker.isin(constituents)], constituents),
                    'fixed_ten': _metrics(control.loc[control.ticker.isin(TRAINING_TICKERS)], TRAINING_TICKERS)}
        report[phase] = summary
    return OpeningGapResult(report, predictions, models)


def run_opening_gap(frame_path, calendar_path, membership_path, output, *, event_path=None, source_fusion=False):
    """Run a sealed local research experiment from explicit, fingerprinted source files."""
    from .direction_audit import load_membership_snapshot
    from .workflows import data_kind

    paths = dict(frame=frame_path, calendar=calendar_path, membership=membership_path)
    if event_path is not None:
        paths['events'] = event_path
    digests = {name: _sha(path) for name, path in paths.items()}
    membership = load_membership_snapshot(membership_path)
    dates = pd.read_csv(calendar_path, usecols=['date']).date
    frame = pd.read_parquet(frame_path)
    if 'feature_version' not in frame or set(frame.feature_version.dropna().unique()) != {FEATURE_VERSION}:
        raise ValueError('Opening research requires the known retrospective feature version')
    symbols = tuple(dict.fromkeys((*TRAINING_TICKERS, *membership['tickers'], *DEFAULT_EVALUATION_TICKERS)))
    events = pd.read_csv(event_path, dtype={'ticker': str}) if event_path is not None else None
    price_kind = data_kind(frame) if 'source' in frame and frame.source.notna().any() else 'unknown'
    evidence_kind = price_kind
    if events is not None and not events.empty:
        declarations = set(events.get('evidence_kind', pd.Series('unknown', index=events.index)).fillna('unknown'))
        if not declarations <= {'historical', 'synthetic', 'unknown'}:
            raise ValueError('Opening event evidence kind must be historical, synthetic or unknown')
        declarations.add(price_kind)
        evidence_kind = ('synthetic' if 'synthetic' in declarations else
                         'unknown' if 'unknown' in declarations else 'historical')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    protocol = dict(task=TASK, target='Raw official daily open strictly above raw prior session close',
                    tie='not_above', missing_answer='wrong', cutoff='08:59:00 Asia/Taipei', hashes=digests,
                    evidence_kind=evidence_kind,
                    runner_sha256=_sha(__file__), membership_as_of=membership['as_of'],
                    score_threshold=.5, selection='Fixed-ten calibration worst-five, then accuracy, then Brier',
                    final_dates=126, calibration_dates=126, min_train_dates=504, max_folds=8, fold_dates=252,
                    source_fusion=source_fusion,
                    training_symbols=TRAINING_TICKERS, transfer_symbols=DEFAULT_EVALUATION_TICKERS,
                    factor_method='ordered_source_probability_deltas',
                    feature_source='08:30 retrospective inputs and optional same-session observed numeric events',
                    source_groups=GROUPS)
    _write_json(output / 'protocol.json', protocol)
    panel = build_opening_gap_panel(frame, dates, symbols, events=events)
    result = validate_opening_gap(panel, membership['tickers'], membership_as_of=membership['as_of'],
                                  evidence_kind=evidence_kind, source_fusion=source_fusion)
    if {name: _sha(path) for name, path in paths.items()} != digests or _sha(__file__) != protocol['runner_sha256']:
        raise RuntimeError('Opening research inputs changed during execution')
    result.predictions.to_parquet(output / 'predictions.parquet', index=False)
    joblib.dump(result.models, output / 'models.joblib')
    result.report['artifacts'] = {name: _sha(output / name) for name in
                                  ('protocol.json', 'predictions.parquet', 'models.joblib')}
    result.report['historical_membership_verified'] = False
    result.report['timely_live_forecasts_verified'] = False
    _write_json(output / 'report.json', result.report)
    return result.report


def _sha(path):
    with open(path, 'rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as file:
        json.dump(json_safe(value), file, indent=2, ensure_ascii=False, allow_nan=False)
