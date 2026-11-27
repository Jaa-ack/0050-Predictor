"""Replay last-three trial directions against the direct 08:59 rule on matched stock-days."""
import argparse
from contextlib import closing
from datetime import date, time
import json
from pathlib import Path
import sqlite3

if __package__:
    from . import opening_trial_rule as rule
else:
    import opening_trial_rule as rule

MODELS = ('direct', 'majority', 'flat_only')


def paths(path, day, symbols):
    if not path.is_file():
        return {}
    cutoff = rule.clock_us(day, rule.DECISION)
    start = rule.clock_us(day, time(8, 58, 30))
    rows = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        for ticker in symbols:
            reference = db.execute("""SELECT previous_close FROM quotes WHERE symbol=? AND source='websocket'
                AND previous_close>0 AND received_us<? ORDER BY received_us DESC LIMIT 1""",
                                   (ticker, cutoff)).fetchone()
            signs = []
            rows[ticker] = signs
            if reference is None:
                continue
            trials = db.execute("""SELECT exchange_us, price FROM trials WHERE symbol=? AND source='websocket'
                AND price>0 AND exchange_us>=? AND exchange_us<=received_us AND received_us<?
                ORDER BY exchange_us DESC, serial DESC, received_us""", (ticker, start, cutoff))
            previous_time = None
            for exchange, price in trials:
                if exchange != previous_time:
                    signs.append((price > reference[0]) - (price < reference[0]))
                    previous_time = exchange
                    if len(signs) == 3:
                        break
    return rows


def forecasts(signs):
    if signs is None:
        return (None,) * 3
    if len(signs) > 3 or any(type(s) is not int or abs(s) > 1 for s in signs):
        raise ValueError('Expected up to three trial directions, latest first')
    direct = bool(signs and signs[0] > 0)
    majority = sum(s > 0 for s in signs) >= 2 if len(signs) == 3 else direct
    return direct, majority, majority if signs and signs[0] == 0 else direct


def collect(root, days, symbols, core):
    from twquant.opening_capture import Archive

    observed, mature = {}, {}
    for day in days:
        archive = Archive(root, day, readonly=True)
        observed[str(day)] = paths(archive.path, day, symbols)
        if archive.path.is_file() and day < days[-1]:
            with archive as db:
                labels = db.panel(core, label_before=rule.clock_us(days[-1], rule.DECISION))
            mature[str(day)] = {r['ticker']: r['actual_up'] for r in labels}
    return dict(observed=observed, calibration_labels=mature, held_out=str(days[-1]))


def score(sessions, symbols, inputs, prices):
    import pandas as pd
    from twquant.opening_gap import _metrics, _paired_hits
    from twquant.universe import TRAINING_TICKERS

    if (len(sessions) < 3 or list(sessions) != sorted(set(sessions)) or inputs['held_out'] != str(sessions[-1])
            or len(symbols) != len(set(symbols)) or not set(TRAINING_TICKERS) <= set(symbols)):
        raise ValueError('Expected prior close, calibration days, final holdout and the full training universe')
    samples = []
    for prev, day in zip(sessions[:-1], sessions[1:], strict=True):
        rows = inputs['observed'].get(str(day), {})
        labels = inputs['calibration_labels'].get(str(day), {})
        for ticker in symbols:
            opening = prices.get(day, {}).get(ticker, (None, None))[0]
            close = prices.get(prev, {}).get(ticker, (None, None))[1]
            signs = rows.get(ticker)
            label = labels.get(ticker) if day < sessions[-1] and ticker in TRAINING_TICKERS else None
            if label is not None and type(label) is not bool:
                raise ValueError('Invalid calibration label')
            samples.append(dict(day=str(day), ticker=ticker, actual_return=opening/close-1 if opening and close else None,
                                mature=float(label) if label is not None else None, depth=len(signs or []),
                                **dict(zip(MODELS, forecasts(signs), strict=True))))
    frame = pd.DataFrame(samples)

    def compare(rows, universe):
        reports = {}
        for name in MODELS:
            model = rows.assign(probability_up=rows[name].astype(float))
            reports[name] = dict(_metrics(model, universe), **_paired_hits(model, rows.direct.astype(float)))
        return reports

    holdout = frame.day.eq(inputs['held_out'])
    calibration = frame.loc[~holdout & frame.ticker.isin(TRAINING_TICKERS)]
    development = compare(calibration.assign(actual_return=calibration.mature), TRAINING_TICKERS)

    def rank(name):
        result = development[name]
        return result['coverage'], min(.75, result['minimum_stock_accuracy']), result['accuracy']

    return dict(evidence_kind='historical_receipt_replay', timely_issued_sessions=0, calibration=development,
                selected=max(MODELS, key=rank), holdout_day=inputs['held_out'],
                holdout=compare(frame.loc[holdout], symbols), full_replay=compare(frame, symbols),
                paths_by_day={d:{k:int(group.depth.eq(k).sum()) for k in range(4)}
                              for d, group in frame.groupby('day')},
                missing_archives=[str(d) for d in sessions[1:] if not inputs['observed'].get(str(d))])


def main():
    if __package__:
        from .audit_opening_receipts import read_bars
    else:
        from audit_opening_receipts import read_bars
    from twquant.direction_audit import load_membership_snapshot
    from twquant.universe import TRAINING_TICKERS

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archives', type=Path)
    source.add_argument('--inputs', type=Path)
    parser.add_argument('--membership', type=Path, required=True)
    parser.add_argument('--bars', type=Path, required=True)
    parser.add_argument('--sessions', nargs='+', type=date.fromisoformat, required=True,
                        help='Consecutive sessions starting with the previous-close date')
    args = parser.parse_args()
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50 or len(args.sessions) < 3:
        parser.error('Expected fifty constituents and at least three ordered trading dates')
    data = (json.loads(args.inputs.read_text()) if args.inputs else
            collect(args.archives.resolve(), args.sessions[1:], symbols, TRAINING_TICKERS))
    prices = {d: read_bars(args.bars / f'{d:%Y%m%d}.json', d) for d in args.sessions}
    print(json.dumps(score(args.sessions, symbols, data, prices), allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
