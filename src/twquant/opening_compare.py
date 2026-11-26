"""Compare the frozen opening classifier and the trial-price correction on one holdout."""
from __future__ import annotations

import argparse
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
from statistics import median
from tempfile import NamedTemporaryFile

if __package__:
    from . import opening_capture as capture
else:
    import capture

from twquant.universe import TRAINING_TICKERS

EVIDENCE = 'chronological_auction_comparison'
TASK = '0859_open_above_previous_session_close'
FUTURE_SESSIONS = 'approved-sessions.json'


def _sample(root, day, symbols, *, label_before=None):
    archive = capture.Archive(root, day, readonly=True)
    if not archive.path.is_file():
        empty = dict(actual_up=None, trial_gap=None, relative_error=None,
                     trial_exchange_us=None, trial_received_us=None)
        return [dict(empty, ticker=ticker) for ticker in symbols]
    cutoff = capture.session_us(day, capture.CUTOFF)
    with archive as store:
        if store.conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('opening_archive_integrity_failed')
        rows = store.panel(symbols, label_before=label_before)
        for row in rows:
            row['relative_error'] = None
            if row['trial_price'] is None:
                continue
            reference, = store.conn.execute(
                """SELECT previous_close FROM quotes WHERE symbol=? AND source='websocket'
                   AND previous_close IS NOT NULL AND received_us<?
                   ORDER BY received_us DESC LIMIT 1""", (row['ticker'], cutoff)).fetchone()
            row['trial_gap'] = (row['trial_price'] - reference) / reference
            if label_before is None or row['actual_up'] is None:
                continue
            opening_reference, price = store.conn.execute(
                """SELECT previous_close, open_price FROM quotes WHERE symbol=? AND open_price IS NOT NULL
                   AND previous_close IS NOT NULL AND received_us<?
                   ORDER BY (source='rest') DESC, received_us DESC LIMIT 1""",
                (row['ticker'], label_before)).fetchone()
            if opening_reference == reference:
                row['relative_error'] = (price - row['trial_price']) / reference
    return rows


def _original(root, symbols, days):
    # The VM's week evaluator keeps its original source and calendar. A replay on
    # an earlier date uses an isolated instance of that same recipe.
    spec = importlib.util.spec_from_file_location('frozen_opening_replay', capture.__file__)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.COLLECTION_DAYS = days
    return replay.evaluate_week(root, symbols)


def _paired(rows, predictions, trial):
    changes = [(guess == row['actual_up'], baseline == row['actual_up'])
               for row, guess, baseline in zip(rows, predictions, trial, strict=True)
               if row['actual_up'] is not None]
    return dict(rescued=sum(hit and not old for hit, old in changes),
                added_errors=sum(old and not hit for hit, old in changes))


def _score(rows, predictions):
    score = capture._summarize(rows, predictions)
    hits = [row['actual_up'] is not None and row['actual_up'] == guess
            for row, guess in zip(rows, predictions, strict=True)]
    n = min(5, len(hits))
    return dict(score, worst_five=sum(sorted(hits)[:n]) / n)


def compare(root, symbols, days, *, original=None):
    """Train on earlier fixed-ten rows and score the same full grid for all methods."""
    if len(days) < 2 or tuple(sorted(set(days))) != days or not symbols:
        raise ValueError('invalid_opening_comparison_calendar')
    cutoff = capture.session_us(days[-1], capture.CUTOFF)
    samples = [_sample(root, day, TRAINING_TICKERS, label_before=cutoff) for day in days[:-1]]
    errors = [[row['relative_error'] for row in day if row['relative_error'] is not None]
              for day in samples]
    train_rows = [error for day in errors for error in day]
    correction = median(train_rows) if train_rows else None
    rows = _sample(root, days[-1], symbols)
    original = original if original is not None else _original(root, symbols, days)
    old = original['validation_rows']
    if (original.get('task') != TASK or original.get('validation_date') != days[-1].isoformat()
            or [(r['ticker'], r['actual_up']) for r in old] != [(r['ticker'], r['actual_up']) for r in rows]):
        raise ValueError('opening_holdout_grid_mismatch')
    trial = [row['trial_gap'] is not None and row['trial_gap'] > 0 for row in rows]
    repaired = []
    for row, baseline in zip(rows, trial, strict=True):
        gap = row['trial_gap']
        adjusted = gap + (correction or 0.) if gap is not None else None
        fallback = ('trial_missing_not_up' if gap is None else
                    'repair_unavailable_trial' if correction is None else None)
        repaired.append(dict(ticker=row['ticker'], actual_up=row['actual_up'], trial_up=baseline,
                            predicted_up=adjusted is not None and adjusted > 0,
                            trial_gap=gap, correction=correction if gap is not None else None,
                            adjusted_gap=adjusted, fallback_reason=fallback,
                            trial_received_us=row['trial_received_us'], trial_exchange_us=row['trial_exchange_us']))
    new_guesses = [row['predicted_up'] for row in repaired]
    old_guesses = [row['predicted_up'] for row in old]
    old_score = _score(rows, old_guesses)
    trial_score = _score(rows, trial)
    if (old_score['correct'] != original['validation']['correct']
            or trial_score['correct'] != original['validation_trial_rule']['correct']):
        raise ValueError('opening_baseline_mismatch')
    return dict(task=TASK, evidence_kind=EVIDENCE, validation_date=days[-1].isoformat(),
                training_tickers=list(TRAINING_TICKERS), original=original, original_score=old_score,
                original_paired=_paired(rows, old_guesses, trial), trial_score=trial_score,
                repair=dict(correction=correction, train_rows=len(train_rows), train_days=sum(map(bool, errors)),
                            rows=repaired, score=_score(rows, new_guesses), paired=_paired(rows, new_guesses, trial)))


def read_future_sessions(path):
    values = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(values, list) or not values:
        raise ValueError('future_opening_sessions_required')
    days = tuple(date.fromisoformat(value) for value in values)
    if tuple(sorted(set(days))) != days or any(day <= capture.COLLECTION_DAYS[-1] or day.weekday() >= 5 for day in days):
        raise ValueError('future_opening_sessions_must_be_ordered_trading_dates')
    return days


def future_units(days):
    """Generate finite continuations of the existing capture and recovery windows."""
    fixed = capture.unit_files()
    units = {}
    for phase, clocks in {'collect': ('08:28..59:00', '09:00..09:00'),
                          'recover': ('13:35,45,55:00',)}.items():
        name = f'twquant-opening-{phase}'
        service = fixed[name + '.service'].replace(
            f'capture.py {phase}', f'opening_compare.py {phase} --sessions {capture.INSTALL}/runtime/{FUTURE_SESSIONS}')
        timer = fixed[name + '.timer']
        schedule = ''.join(f'OnCalendar={day.isoformat()} {clock} Asia/Taipei\n'
                           for day in days for clock in clocks)
        timer = ''.join(line for line in timer.splitlines(keepends=True) if not line.startswith('OnCalendar='))
        units[f'twquant-opening-future-{phase}.service'] = service
        units[f'twquant-opening-future-{phase}.timer'] = timer.replace('[Timer]\n', '[Timer]\n' + schedule)
    return units


def _save(path, report):
    # Publish a private replay only after serialization succeeds.
    with NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(report, file, allow_nan=False, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('compare', 'collect', 'recover', 'units'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--holdout', type=date.fromisoformat)
    parser.add_argument('--sessions', type=Path)
    args = parser.parse_args(argv)
    if not args.root.is_absolute():
        parser.error('An absolute private root is required')
    os.umask(0o077)
    try:
        if args.mode != 'compare':
            if args.sessions is None or not args.sessions.is_absolute():
                parser.error('An absolute approved session calendar is required')
            days = read_future_sessions(args.sessions)
            if args.mode == 'units':
                if args.root != Path(capture.INSTALL) / 'runtime' or args.sessions != args.root / FUTURE_SESSIONS:
                    raise ValueError('opening_units_require_vm_paths')
                print(json.dumps(future_units(days)))
                return 0
            capture.COLLECTION_DAYS = days
            return capture.main([args.mode, '--root', str(args.root)])
        now = capture._now()
        holdout = args.holdout or capture.COLLECTION_DAYS[-1]
        calendar = capture.COLLECTION_DAYS
        if args.sessions is not None:
            if not args.sessions.is_absolute():
                parser.error('An absolute approved session calendar is required')
            calendar += read_future_sessions(args.sessions)
        if (holdout not in calendar[1:] or holdout > now.date()
                or (holdout == now.date() and now.time() < capture.COLLECT_END)):
            raise ValueError('opening_comparison_not_ready')
        days = tuple(day for day in calendar if day <= holdout)
        symbols = tuple(json.loads((args.root / 'symbols.json').read_text()))
        if (len(symbols) != 50 or len(set(symbols)) != 50 or not set(TRAINING_TICKERS) <= set(symbols)
                or not all(isinstance(ticker, str) and len(ticker) == 4 and ticker.isascii()
                           and ticker.isdecimal() for ticker in symbols)):
            raise ValueError('invalid_fixed_constituent_set')
        with capture._lock(args.root / 'opening.lock') as locked:
            if not locked:
                return 1
            official = (json.loads((args.root / 'evaluation.json').read_text())
                        if holdout == capture.COLLECTION_DAYS[-1] else None)
            _save(args.root / f'comparison-{holdout.isoformat()}.json', compare(args.root, symbols, days, original=official))
        return 0
    except Exception as exc:
        print(f'opening_{args.mode}_failed:{type(exc).__name__}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
