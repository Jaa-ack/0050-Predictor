"""Read-only projections of sealed opening research and scheduled capture archives."""
from datetime import date, timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import sqlite3

import pandas as pd
import pyarrow.parquet as parquet

from .opening_capture import Archive, COLLECTION_DAYS
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


@lru_cache(maxsize=4)
def _history_days(path, version, tickers):
    frame = _day_frame(pd.read_parquet(path, columns=['date', 'ticker', 'phase', 'actual_return',
                                                      'probability_up']), tickers)
    return [
        dict(date=day, accuracy=float(rows.hit.mean()), expected=len(rows), hits=int(rows.hit.sum()),
             known=int(rows.actual_return.notna().sum()))
        for day, rows in frame.groupby('day')
    ]


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

    def summary(self):
        report, path = self._history()
        week = self._week()
        history = dict(evidence=report.get('evidence_kind'), status='ready' if path else 'unavailable', days=[])
        if path:
            final = report['final']['constituents']
            history.update(accuracy=_number(final.get('overall_accuracy')), worst_five=_number(final.get('worst_five_accuracy')),
                           expected=final.get('expected_predictions'), sessions=final.get('test_dates'),
                           sources={key: _number(value) for key, value in report.get('source_coverage', {}).get('constituents', {}).items()
                                    if key in GROUPS},
                           days=_history_days(path, path.stat().st_mtime_ns, tuple(final['evaluation_tickers'])))
        planned = self._capture_days(week)
        score = week.get('validation', {})
        evaluation_day = week.get('validation_date')
        trial_rule = week.get('validation_trial_rule', {})
        return dict(task=TASK, target=TARGET, history=history,
                    week=dict(evidence=week.get('evidence_kind'), days=planned, date=evaluation_day,
                              status='ready' if week else 'collecting' if any(day['inputs'] is not None for day in planned) else 'scheduled',
                              accuracy=_number(score.get('accuracy')), expected=score.get('expected'),
                              known=score.get('expected', 0) - score.get('unknown_answers', 0) if week else None,
                              training_rows=week.get('train_rows'), fitted=week.get('model') is not None if week else None,
                              trial_rule=_number(trial_rule.get('accuracy')),
                              always_not_up=_number(week.get('validation_fixed_not_up', {}).get('accuracy'))))

    def _capture_days(self, week):
        saved = week.get('collection', {})
        symbols = tuple(_json(self.capture / 'symbols.json')) if self.capture else ()
        days = []
        for day in COLLECTION_DAYS:
            iso = day.isoformat()
            count = saved.get(iso, {}).get('timed_trial_inputs')
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
        if view == 'week':
            report = self._week()
            reported = report.get('validation_date')
            rows = report.get('validation_rows', []) if not day or day.isoformat() == reported else []
            return dict(date=reported if not day else day.isoformat(), rows=[_week_row(row) for row in rows])
        report, path = self._history()
        if not path:
            return dict(date=None, rows=[])
        tickers = tuple(report['final']['constituents']['evaluation_tickers'])
        dates = _history_days(path, path.stat().st_mtime_ns, tickers)
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
                           features=[dict(key=key, value=round(value * .029, 4) if group != 'volume'
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
    weekly = {COLLECTION_DAYS[-1].isoformat(): generate(specs[-1:])}
    all_history = [row for rows in constituent_rows.values() for row in rows]
    ranked = sorted(sum(row['correct'] for row in all_history if row['ticker'] == ticker) / len(days)
                    for ticker in symbols)
    week_day, week_rows = next(iter(weekly.items()))
    rule = sum((row['groups'][0]['features'][0]['value'] > 0) == row['actual_up'] for row in week_rows) / len(week_rows)
    not_up = sum(not row['actual_up'] for row in week_rows) / len(week_rows)
    return dict(task=TASK, target=TARGET,
                history=dict(evidence='synthetic', status='ready', sessions=len(days), **_summary(all_history),
                             worst_five=sum(ranked[:5]) / 5, sources={group: 1. for group, *_ in specs},
                             days=[dict(date=day, **_summary(rows)) for day, rows in constituent_rows.items()]),
                week=dict(evidence='synthetic', status='ready', date=week_day, training_rows=40, fitted=True,
                          **_summary(week_rows), trial_rule=rule, always_not_up=not_up,
                          days=[dict(date=day.isoformat(), inputs=count, expected=50)
                                for day, count in zip(COLLECTION_DAYS, [44, 48, 47, 50, 50], strict=True)]),
                rows=dict(history=history_rows, week=weekly))
