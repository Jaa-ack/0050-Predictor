"""Read-only projections of sealed opening research and scheduled capture archives."""
from datetime import date, timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import re
import sqlite3

import pandas as pd
import pyarrow.parquet as parquet

from .opening_capture import Archive, COLLECTION_DAYS
from .opening_compare import EVIDENCE as COMPARISON_EVIDENCE
from .opening_gap import GROUPS, TASK, TARGET
from .universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS


def _json(path):
    if path and path.is_file():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return {}


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _summary(rows):
    count = len(rows)
    hit = sum(row['correct'] is True for row in rows)
    valid = sum(row['actual_up'] is not None for row in rows)
    return dict(expected=count, hits=hit, known=valid, accuracy=hit / count if count else None)


def _day_frame(frame, tickers):
    frame = frame.loc[frame.phase.eq('final') & frame.ticker.isin(tickers)].copy()
    return frame.assign(day=frame.date.dt.strftime('%Y-%m-%d'),
                        hit=frame.actual_return.notna() & frame.probability_up.notna()
                            & frame.actual_return.gt(0).eq(frame.probability_up.ge(.5)))


def _diagnostics(frame, groups, *, coefficients=None):
    """Describe observed inputs and fitted model behavior on the displayed sample."""
    result = {}
    coefficients = coefficients or {}
    target = frame['actual_up']
    known = target.notna()
    for group, keys in groups.items():
        columns = [key for key in keys if key in frame]
        features = []
        for key in columns:
            series = pd.to_numeric(frame[key], errors='coerce')
            pair = known & series.notna()
            values = series[pair]
            outcomes = target[pair]
            matched = len(values)
            r = float(values.corr(outcomes.astype(float))) if matched >= 3 and values.nunique() > 1 and outcomes.nunique() > 1 else None
            features.append(dict(key=key, samples=matched, correlation=r, coefficient=coefficients.get(key)))
        counterpart = frame.get(f'impact_{group}')
        impact = _number(float(counterpart.abs().mean())) if counterpart is not None else None
        result[group] = dict(features=features, impact=impact,
                             coverage=float(frame[columns].notna().any(axis=1).mean()) if columns and len(frame) else None)
    denominator = sum(info['impact'] or 0. for info in result.values())
    for info in result.values():
        info['impact_share'] = info['impact'] / denominator if info['impact'] is not None and denominator else None
    return result


@lru_cache(maxsize=4)
def _history_check(path, version, tickers):
    columns = ['date', 'ticker', 'phase', 'actual_return', 'probability_up']
    known = set(parquet.ParquetFile(path).schema_arrow.names)
    inputs = [key for group, group_cols in GROUPS.items() for key in (f'impact_{group}', *group_cols) if key in known]
    frame = _day_frame(pd.read_parquet(path, columns=columns + inputs, filters=[('phase', '=', 'final')]), tickers)
    days = [
        dict(date=day, accuracy=float(rows.hit.mean()), expected=len(rows), hits=int(rows.hit.sum()),
             known=int(rows.actual_return.notna().sum()))
        for day, rows in frame.groupby('day')
    ]
    frame['actual_up'] = frame.actual_return.gt(0).astype(float).where(frame.actual_return.notna())
    return days, _diagnostics(frame, GROUPS)


def _week_diagnostics(report):
    model = report.get('model') or {}
    rows = []
    features = model.get('features', [])
    coefficients = dict(zip(features, model.get('coefficients', []), strict=True)) if features else {}
    for row in report.get('validation_rows', []):
        factors = row.get('factors') or []
        rows.append(dict(actual_up=row.get('actual_up'), **{factor['key']: factor['value'] for factor in factors},
                         **{f"impact_{factor['key']}": factor['impact'] for factor in factors},
                         impact_auction=sum(factor['impact'] for factor in factors)))
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    return {**_diagnostics(frame, {'auction': features}, coefficients=coefficients),
            **_diagnostics(frame, {key: [key] for key in features}, coefficients=coefficients)}


def _repair_diagnostics(rows):
    frame = pd.DataFrame([dict(actual_up=row['actual_up'], trial_gap=row['trial_gap']) for row in rows])
    return _diagnostics(frame, {'trial_gap': ['trial_gap']}) if len(frame) else {}


def _week_row(row):
    actual, prediction = row.get('actual_up'), row.get('predicted_up')
    factors = [dict(key=item['key'], value=_number(item.get('value')), impact=_number(item.get('impact')),
                    imputed=item.get('imputed') is True)
               for item in row.get('factors', []) if item.get('key') in {'trial_gap', 'log_matched_size', 'book_imbalance'}]
    return dict(ticker=str(row['ticker']), actual_up=actual, predicted_up=prediction,
                probability=_number(row.get('probability_up')), correct=prediction == actual if actual is not None else None,
                factor_base=_number(row.get('factor_base')), groups=[dict(group='auction', features=factors,
                    impact=sum(item['impact'] or 0. for item in factors))] if factors else [],
                fallback=row.get('fallback_reason'), exchange_us=row.get('trial_exchange_us'),
                receipt_us=row.get('trial_received_us'), model='auction_logistic')


def _repair_row(row):
    actual, prediction = row['actual_up'], row['predicted_up']
    return dict(ticker=str(row['ticker']), actual_up=actual, predicted_up=prediction,
                probability=None, correct=prediction == actual if actual is not None else None,
                trial_up=row['trial_up'], trial_gap=_number(row['trial_gap']),
                correction=_number(row['correction']), adjusted_gap=_number(row['adjusted_gap']),
                exchange_us=row.get('trial_exchange_us'), receipt_us=row.get('trial_received_us'),
                fallback=row.get('fallback_reason'), model='auction_repair')


def _class_counts(rows):
    return dict(up=sum(row['actual_up'] is True for row in rows),
                not_up=sum(row['actual_up'] is False for row in rows),
                unknown=sum(row['actual_up'] is None for row in rows))


def _paired_window(reports):
    dates = sorted(reports)
    window = dict(dates=dates, sessions=len(dates), worst_five={})
    if len(dates) < 2:
        return window
    totals = {}
    for day in dates:
        report = reports[day]
        original = {row['ticker']: row['predicted_up'] for row in report['original']['validation_rows']}
        rows = {row['ticker']: row for row in report['repair']['rows']}
        if not rows or original.keys() != rows.keys() or (totals and totals.keys() != rows.keys()):
            return window
        for ticker, row in rows.items():
            label = row['actual_up']
            hits = totals.setdefault(ticker, [0, 0, 0])
            for i, guess in enumerate((original[ticker], row['trial_up'], row['predicted_up'])):
                hits[i] += label is not None and guess == label
    n = min(5, len(totals))
    window['worst_five'] = {method: sum(sorted(hits[i] for hits in totals.values())[:n]) / (n * len(dates))
                            for i, method in enumerate(('original', 'trial', 'repair'))}
    return window


class OpeningWorkspace:
    """Expose numeric projections only from server-configured, private directories."""

    def __init__(self, study, capture):
        self.study = Path(study).resolve() if study else None
        self.capture = Path(capture).resolve() if capture else None

    def _history(self):
        if self.study is None:
            return {}, None
        report = _json(self.study / 'report.json')
        path = self.study / 'predictions.parquet'
        if report.get('task') != TASK or not path.is_file():
            return {}, None
        return report, path

    def _week(self):
        report = _json(self.capture / 'evaluation.json') if self.capture else {}
        if report.get('task') != TASK or report.get('evidence_kind') != 'short_chronological_capture_replay':
            return {}
        return report

    def _comparisons(self):
        if self.capture is None:
            return {}
        reports = {}
        for path in sorted(self.capture.glob('comparison-*.json')):
            match = re.fullmatch(r'comparison-(\d{4}-\d{2}-\d{2})\.json', path.name)
            if not match:
                continue
            iso = match[1]
            report = _json(path)
            if (report.get('task') == TASK and report.get('evidence_kind') == COMPARISON_EVIDENCE
                    and report.get('validation_date') == iso):
                reports[iso] = report
        return reports

    def _latest_week(self, comparison):
        return max(self._week(), comparison.get('original', {}), key=lambda report: report.get('validation_date', ''))

    def summary(self):
        report, path = self._history()
        comparisons = self._comparisons()
        latest = next(reversed(comparisons.values()), {})
        week = self._latest_week(latest)
        history = dict(evidence=report.get('evidence_kind'), status='ready' if path else 'unavailable', days=[])
        if path:
            final = report['final']['constituents']
            days, diagnostics = _history_check(path, path.stat().st_mtime_ns, tuple(final['evaluation_tickers']))
            history.update(accuracy=_number(final.get('overall_accuracy')), worst_five=_number(final.get('worst_five_accuracy')),
                           expected=final.get('expected_predictions'), sessions=final.get('test_dates'),
                           sources={key: _number(value) for key, value in report.get('source_coverage', {}).get('constituents', {}).items()
                                    if key in GROUPS},
                           days=days, diagnostics=diagnostics)
            controls = report['final'].get('controls', {})
            history['models'] = {}
            for name in ('context', 'context_tree', 'source_fusion'):
                if name in controls:
                    model = controls[name]['constituents']
                    history['models'][name] = {key: _number(model.get(key))
                                               for key in ('accuracy', 'worst_five_accuracy', 'minimum_stock_accuracy', 'expected')}
            fold = next((item for item in reversed(report.get('folds', [])) if item.get('phase') == 'final'), {})
            history['source_weights'] = {group: _number(values.get('probability'))
                                         for group, values in fold.get('source_fusion_weights', {}).items()
                                         if group in GROUPS}
        planned = self._capture_days(week)
        score = week.get('validation', {})
        evaluation_day = week.get('validation_date')
        trial_rule = week.get('validation_trial_rule', {})
        repair = latest.get('repair', {})
        repair_score = repair.get('score', {})
        status = 'collecting' if any(day['inputs'] is not None for day in planned) else 'scheduled'
        paired = {}
        for day, result in comparisons.items():
            variant = result['repair']
            paired[day] = dict(original=result['original_score'], repair=variant['score'], trial=result['trial_score'],
                               classes=_class_counts(variant['rows']),
                               original_paired=result['original_paired'], repair_paired=variant['paired'],
                               original_diagnostics=_week_diagnostics(result['original']),
                               repair_diagnostics=_repair_diagnostics(variant['rows']),
                               original_training_rows=result['original']['train_rows'],
                               original_fitted=result['original']['model'] is not None,
                               repair_training_rows=variant['train_rows'], correction=variant['correction'])
        return dict(task=TASK, target=TARGET, history=history,
                    week=dict(evidence=week.get('evidence_kind'), days=planned, date=evaluation_day,
                              status='ready' if week else status,
                              accuracy=_number(score.get('accuracy')), expected=score.get('expected'),
                              known=score.get('expected', 0) - score.get('unknown_answers', 0) if week else None,
                              training_rows=week.get('train_rows'), fitted=week.get('model') is not None if week else None,
                              diagnostics=_week_diagnostics(week),
                              trial_rule=_number(trial_rule.get('accuracy')),
                              always_not_up=_number(week.get('validation_fixed_not_up', {}).get('accuracy'))),
                    repair=dict(evidence=latest.get('evidence_kind'), days=planned, date=latest.get('validation_date'),
                                status='ready' if latest else status, accuracy=_number(repair_score.get('accuracy')),
                                expected=repair_score.get('expected'), training_rows=repair.get('train_rows'),
                                correction=_number(repair.get('correction')),
                                diagnostics=_repair_diagnostics(repair.get('rows', []))),
                    comparisons=paired, paired_window=_paired_window(comparisons))

    def _capture_days(self, week):
        saved = week.get('collection', {})
        status = _json(self.capture / 'capture-status.json') if self.capture else {}
        observed = status.get('days', {}) if status.get('evidence') == 'vm_observed' else {}
        symbols = tuple(_json(self.capture / 'symbols.json')) if self.capture else ()
        days = []
        for iso in sorted({*(day.isoformat() for day in COLLECTION_DAYS), *saved}):
            day = date.fromisoformat(iso)
            count = saved.get(iso, {}).get('timed_trial_inputs')
            if count is None:
                observed_count = observed.get(iso, {}).get('timed_trial_inputs')
                if type(observed_count) is int and 0 <= observed_count <= (len(symbols) or 50):
                    count = observed_count
            if count is None and self.capture and symbols:
                archive = Archive(self.capture, day, readonly=True)
                if archive.path.is_file():
                    try:
                        with archive as store:
                            count = sum(row['trial_gap'] is not None for row in store.panel(symbols))
                    except sqlite3.OperationalError:
                        # A copied SQLite may arrive before its final tables or journal have been copied.
                        pass
            days.append(dict(date=iso, inputs=count, expected=len(symbols) or 50))
        return days

    def predictions(self, view, day):
        if view in ('week', 'repair'):
            comparisons = self._comparisons()
            selected = comparisons.get(day.isoformat(), {}) if day else next(reversed(comparisons.values()), {})
            if view == 'repair':
                reported = day.isoformat() if day else selected.get('validation_date')
                rows = selected.get('repair', {}).get('rows', [])
                return dict(date=reported, rows=[_repair_row(row) for row in rows])
            report = selected.get('original', {}) if day else self._latest_week(selected)
            if not report:
                report = self._week()
            reported = report.get('validation_date')
            rows = report.get('validation_rows', []) if not day or day.isoformat() == reported else []
            return dict(date=reported if not day else day.isoformat(), rows=[_week_row(row) for row in rows])
        report, path = self._history()
        if not path:
            return dict(date=None, rows=[])
        tickers = tuple(report['final']['constituents']['evaluation_tickers'])
        dates, _ = _history_check(path, path.stat().st_mtime_ns, tickers)
        target = day.isoformat() if day else dates[-1]['date'] if dates else None
        if not target or not any(item['date'] == target for item in dates):
            return dict(date=target, rows=[])
        known = ['date', 'ticker', 'phase', 'actual_return', 'probability_up', 'model_version', 'factor_base',
                 *[f'impact_{group}' for group in GROUPS], *[name for names in GROUPS.values() for name in names]]
        available = set(parquet.ParquetFile(path).schema_arrow.names)
        data = pd.read_parquet(path, columns=[col for col in known if col in available],
                               filters=[('date', '=', pd.Timestamp(target)), ('phase', '=', 'final')])
        models = {fold['model_version']: fold for fold in report.get('folds', [])}
        rows = []
        displayed = (*tickers, *DEFAULT_EVALUATION_TICKERS)
        for record in _day_frame(data, displayed).to_dict('records'):
            actual = _number(record['actual_return'])
            probability = _number(record['probability_up'])
            prediction = probability >= .5 if probability is not None else None
            fold = models.get(record.get('model_version'), {})
            name = fold.get('model')
            columns = fold.get('candidate_features', {}).get(name, ())
            base = _number(record.get('factor_base'))
            groups = [
                dict(group=group, impact=_number(record.get(f'impact_{group}')),
                     features=[dict(key=feature, value=_number(record.get(feature)))
                               for feature in features if feature in columns])
                for group, features in GROUPS.items()
                if base is not None and any(feature in columns for feature in features)
            ]
            ticker = record['ticker']
            rows.append(dict(ticker=ticker, constituent=ticker in tickers, transfer=ticker in DEFAULT_EVALUATION_TICKERS,
                             model=name, actual_up=actual > 0 if actual is not None else None,
                             actual_return=actual, predicted_up=prediction, probability=probability,
                             correct=bool(record['hit']) if actual is not None and prediction is not None else None,
                             factor_base=base, groups=groups,
                             fallback='inputs_missing' if groups and all(feature['value'] is None
                                        for group in groups for feature in group['features']) else None))
        return dict(date=target, rows=rows)


def synthetic_opening():
    """Deterministic fictional score and factor data for the static preview."""
    rng = random.Random(5059)
    # Known stock identifiers for navigation. Every value and outcome below is generated.
    symbols = (*TRAINING_TICKERS, '1216', '2002', '2412', '2303', '2301', '2327', '2345', '2357', '2379',
               '2408', '2603', '2609', '2615', '2882', '2884', '2885', '2886', '2887', '2890', '2892',
               '3008', '3017', '3045', '3231', '3443', '3661', '3665', '3702', '4904', '4938', '5880',
               '6415', '6446', '6505', '6669', '6781', '6919', '7769', '8046', '9910')
    specs = [('price', 'f_return_1', .04), ('volume', 'f_rvol_20', .06),
             ('us', 'f_ext_fmp_tsm_window_return', .08), ('night', 'f_ext_taifex_tx_night_return', .13),
             ('auction', 'trial_gap', .16)]

    def generate(factors, *, transfers=False):
        base = .51
        rows = []
        displayed = tuple(dict.fromkeys((*symbols, *DEFAULT_EVALUATION_TICKERS))) if transfers else symbols
        for ticker in displayed:
            value = rng.uniform(-1, 1)
            groups = [dict(group=group, impact=round(weight * (value * .56 + rng.uniform(-.44, .44)), 4),
                           features=[dict(key=key, value=round(5 + value, 2) if key == 'log_matched_size'
                                          else round(value * .029, 4) if group != 'volume'
                                          else round(1 + value * .4, 2))]) for group, key, weight in factors]
            p = base + sum(item['impact'] for item in groups)
            guess = p >= .5
            actual = guess if rng.random() < .72 else not guess
            rows.append(dict(ticker=ticker, constituent=ticker in symbols,
                             transfer=transfers and ticker in DEFAULT_EVALUATION_TICKERS,
                             actual_up=actual, predicted_up=guess, probability=p,
                             correct=guess == actual, factor_base=base, groups=groups, model='synthetic_model'))
        return rows

    end = date(2026, 11, 20)
    days = [(end - timedelta(days=i)).isoformat() for i in range(24, -1, -1)
            if (end - timedelta(days=i)).weekday() < 5]
    history_rows = {day: generate(specs, transfers=True) for day in days}
    constituent_rows = {day: [row for row in rows if row['constituent']] for day, rows in history_rows.items()}
    all_history = [row for rows in constituent_rows.values() for row in rows]
    ranked = sorted(sum(row['correct'] for row in all_history if row['ticker'] == ticker) / len(days)
                    for ticker in symbols)
    def score(sample):
        counts = _summary(sample)
        recall = {}
        for direction, label in ((True, 'up'), (False, 'not_up')):
            cohort = [row['correct'] for row in sample if row['actual_up'] is direction]
            recall[label] = sum(cohort) / len(cohort) if cohort else None
        return dict(accuracy=counts['accuracy'], expected=len(sample), correct=counts['hits'],
                    worst_five=max(0, counts['hits'] - (len(sample) - 5)) / 5,
                    unknown_answers=0, recall=recall)

    def paired(sample, baseline):
        return dict(rescued=sum(row['correct'] and not old['correct'] for row, old in zip(sample, baseline, strict=True)),
                    added_errors=sum(old['correct'] and not row['correct'] for row, old in zip(sample, baseline, strict=True)))

    def auction_day():
        def probability(logit):
            return 1 / (1 + math.exp(-logit))

        original, baseline, repaired, diagnostic = [], [], [], []
        correction = -.0018
        keys = ('trial_gap', 'log_matched_size', 'book_imbalance')
        weights = (.90, .12, .35)
        for ticker in symbols:
            gap = rng.uniform(-.013, .015)
            size = rng.uniform(4, 6)
            imbalance = rng.uniform(-.85, .85)
            actual = gap + correction + .0012 * imbalance + rng.gauss(0, .0035) > 0
            logit = -.10
            starting = p = probability(logit)
            factors = []
            for key, value, centered, weight in zip(keys, (gap, size, imbalance),
                                                    (gap / .012, size - 5, imbalance / .5), weights, strict=True):
                logit += weight * centered
                updated = probability(logit)
                factors.append(dict(key=key, value=value, impact=updated - p))
                p = updated
            record = dict(ticker=ticker, actual_up=actual, constituent=True, transfer=False)
            original.append(dict(record, predicted_up=p > .5, probability=p, correct=(p > .5) == actual,
                                 factor_base=starting, model='auction_logistic',
                                 groups=[dict(group='auction', features=factors, impact=p - starting)]))
            baseline.append(dict(record, predicted_up=gap > 0, correct=(gap > 0) == actual))
            row = dict(record, trial_up=gap > 0, trial_gap=gap, correction=correction,
                       adjusted_gap=gap + correction, predicted_up=gap + correction > 0)
            repaired.append(dict(_repair_row(row), constituent=True, transfer=False))
            diagnostic.append(dict(actual_up=actual, factors=factors))
        stats = _week_diagnostics(dict(validation_rows=diagnostic, model=dict(features=keys, coefficients=weights)))
        comparison = dict(original=score(original), repair=score(repaired), trial=score(baseline),
                          classes=_class_counts(repaired), original_paired=paired(original, baseline),
                          repair_paired=paired(repaired, baseline), original_diagnostics=stats,
                          repair_diagnostics=_repair_diagnostics(repaired), original_training_rows=None,
                          original_fitted=True, repair_training_rows=None, correction=correction)
        return original, repaired, comparison

    demo_days = tuple(date(2026, 11, day).isoformat() for day in range(9, 14))
    days_covered = [dict(date=day, inputs=50, expected=50) for day in demo_days]
    captures = {day: auction_day() for day in demo_days[-3:]}
    weekly = {day: result[0] for day, result in captures.items()}
    repairs = {day: result[1] for day, result in captures.items()}
    comparisons = {day: result[2] for day, result in captures.items()}
    window = _paired_window({day: dict(original=dict(validation_rows=weekly[day]),
                                       repair=dict(rows=repairs[day])) for day in captures})
    week_day = demo_days[-1]
    week_rows, repaired, comparison = captures[week_day]
    def diagnostics(rows, groups):
        values = [dict(actual_up=row['actual_up'],
                       **{f"impact_{group['group']}": group['impact'] for group in row['groups']},
                       **{item['key']: item['value'] for group in row['groups'] for item in group['features']})
                  for row in rows]
        return _diagnostics(pd.DataFrame(values), groups)
    return dict(task=TASK, target=TARGET,
                history=dict(evidence='synthetic', status='ready', sessions=len(days), **_summary(all_history),
                             worst_five=sum(ranked[:5]) / 5, sources={group: 1. for group, *_ in specs},
                             historical_inputs={'other': GROUPS['other']},
                             diagnostics=diagnostics(all_history, {group: [key] for group,key,_ in specs}),
                             days=[dict(date=day, **_summary(rows)) for day, rows in constituent_rows.items()]),
                week=dict(evidence='synthetic', status='ready', date=week_day, training_rows=None, fitted=True,
                          **_summary(week_rows), trial_rule=comparison['trial']['accuracy'],
                          always_not_up=sum(not row['actual_up'] for row in week_rows) / len(week_rows),
                          diagnostics=comparison['original_diagnostics'], days=days_covered),
                repair=dict(evidence='synthetic', status='ready', date=week_day, training_rows=None,
                            **_summary(repaired), diagnostics=comparison['repair_diagnostics'],
                            correction=comparison['correction'], days=days_covered),
                comparisons=comparisons, paired_window=window,
                rows=dict(history=history_rows, week=weekly, repair=repairs))
