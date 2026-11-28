"""Replay archived WebSocket arrivals into a temporary WAL while timing 50-stock delivery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time

from twquant.opening_capture import SCHEMA

if __package__:
    from . import opening_trial_rule as rule
else:
    import opening_trial_rule as rule

WINDOW_US = 2_000_000
FLUSH_US = 200_000
RECIPE = 'historical_receipts_concurrent_wal_rehearsal_v1'


def prepare(source, scratch, cutoff):
    start = cutoff - WINDOW_US
    tail = []
    with closing(sqlite3.connect(scratch, uri=True)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript(SCHEMA)
        db.execute('ATTACH DATABASE ? AS history', (source.resolve().as_uri() + '?mode=ro',))
        for table in ('quotes', 'trials'):
            with db:
                db.execute(f"""INSERT INTO {table} SELECT * FROM history.{table}
                    WHERE source='websocket' AND received_us<? ORDER BY rowid""", (start,))
            data = db.execute(f"""SELECT received_us, * FROM history.{table}
                WHERE source='websocket' AND received_us>=? AND received_us<? ORDER BY rowid""",
                              (start, cutoff+FLUSH_US))
            for received, *values in data:
                tail.append((received, table, values))
    return sorted(tail, key=lambda event: event[0])


def flush(writer, tail, cursor, stop):
    with writer:
        while cursor < len(tail) and tail[cursor][0] < stop:
            _, table, values = tail[cursor]
            marks = ','.join('?' for _ in values)
            writer.execute(f'INSERT INTO {table} VALUES ({marks})', values)
            cursor += 1
    return cursor


def wait_until(tick_ns):
    while (remaining := tick_ns-time.monotonic_ns()) > 0:
        time.sleep(remaining / 1e9)


def play(scratch, tail, start, origin_ns, phase_us):
    with closing(sqlite3.connect(scratch)) as writer:
        writer.execute('PRAGMA synchronous=FULL')
        cursor = 0
        for offset in range(phase_us, WINDOW_US+2*FLUSH_US, FLUSH_US):
            wait_until(origin_ns + offset*1000)
            cursor = flush(writer, tail, cursor, start+offset)
        if cursor != len(tail):
            raise RuntimeError('Incomplete replay of archived arrivals')


def read_rows(path, day, symbols, reserve_us):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if reserve_us:
            stop = rule.clock_us(day, rule.DECISION) - reserve_us
            for table in ('quotes', 'trials'):
                db.execute(f'CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE received_us < {stop:d}')
        return rule.directions(db, day, symbols)


def run(source, temp_root, day, symbols, reserve_us, phase_us):
    cutoff = rule.clock_us(day, rule.DECISION)
    if temp_root.stat().st_dev != source.stat().st_dev:
        raise ValueError('Measure the same filesystem used for capture')
    with TemporaryDirectory(dir=temp_root) as private:
        scratch = Path(private) / 'stream.sqlite'
        inbox = Path(private) / 'rehearsal.sqlite'
        tail = prepare(source, scratch, cutoff)
        direct = read_rows(source, day, symbols, 0)
        ideal = read_rows(source, day, symbols, reserve_us)
        with closing(sqlite3.connect(inbox)) as sender, ThreadPoolExecutor(max_workers=1) as pool:
            sender.execute('PRAGMA synchronous=FULL')
            sender.execute('CREATE TABLE rehearsal (payload BLOB NOT NULL, fingerprint TEXT NOT NULL)')
            origin_ns = time.monotonic_ns() + 50_000_000
            due = origin_ns + WINDOW_US*1000
            job = pool.submit(play, scratch, tail, cutoff-WINDOW_US, origin_ns, phase_us)
            wait_until(due-reserve_us*1000)
            rows = read_rows(scratch, day, symbols, reserve_us)
            body = json.dumps(dict(evidence_kind=RECIPE, day=str(day), rows=rows),
                              separators=(',', ':'), allow_nan=False).encode()
            with sender:
                sender.execute('INSERT INTO rehearsal VALUES (?,?)', (body,rule.digest(body)))
            with closing(sqlite3.connect(inbox.as_uri()+'?mode=ro', uri=True)) as receiver:
                payload, fingerprint = receiver.execute('SELECT payload, fingerprint FROM rehearsal').fetchone()
                visible = json.loads(payload)
                if (rule.digest(payload) != fingerprint or visible['evidence_kind'] != RECIPE
                        or visible['day'] != str(day) or [r['ticker'] for r in visible['rows']] != list(symbols)):
                    raise ValueError('Incomplete rehearsal readback')
                readback_delay = time.monotonic_ns() - due
            job.result()
    observed = [dict(ticker=a['ticker'], direct=a['up'], replayed=b['up'],
                     direct_fallback=a['fallback'], replayed_fallback=b['fallback'])
                for a,b in zip(direct, visible['rows'], strict=True)]
    return dict(session=str(day), reserve_us=reserve_us, phase_us=phase_us, receipt_delay_ns=readback_delay,
                archived_updates=len(tail), evidence_changes=sum(a!=b for a,b in zip(ideal, rows, strict=True)),
                observed=observed)


def score(days, symbols, prices, cases, phase_us, reserve_us):
    import pandas as pd
    from twquant.opening_gap import _metrics, _paired_hits

    if len(days) < 2 or list(days) != sorted(set(days)):
        raise ValueError('Expected consecutive trading dates starting with prior close')
    selected = [c for c in cases if c['phase_us'] == phase_us and c['reserve_us'] == reserve_us]
    by_day = {c['session']: c for c in selected}
    if len(selected) != len(by_day) or set(by_day) - {str(d) for d in days[1:]}:
        raise ValueError('Duplicate or unexpected rehearsal session')
    samples, missing, delays = [], [], []
    for previous, day in zip(days[:-1], days[1:], strict=True):
        case = by_day.get(str(day))
        observed = {}
        timely = False
        if case is None:
            missing.append(str(day))
        else:
            rows = case['observed']
            if ([r['ticker'] for r in rows] != list(symbols) or type(case['receipt_delay_ns']) is not int
                    or any(type(r[k]) is not bool for r in rows for k in ('direct','replayed'))):
                raise ValueError('Invalid rehearsal readback or grid')
            delays.append(case['receipt_delay_ns'])
            timely = delays[-1] <= 0
            observed = {r['ticker']: r for r in rows}
        for ticker in symbols:
            opening = prices.get(day, {}).get(ticker, (None, None))[0]
            close = prices.get(previous, {}).get(ticker, (None, None))[1]
            row = observed.get(ticker, {})
            samples.append(dict(ticker=ticker, actual_return=opening/close-1 if opening and close else None,
                                completed_archive=row.get('direct'), direction=row.get('replayed'),
                                timing_gated=row.get('replayed') if timely else None))
    frame = pd.DataFrame(samples)
    baseline = frame.completed_archive.astype(float)
    result = dict(phase_us=phase_us, reserve_us=reserve_us, missing_days=missing,
                  timely_rereads=sum(delay <= 0 for delay in delays), observed_runs=len(delays),
                  earliest_delay_ns=min(delays, default=None), latest_delay_ns=max(delays, default=None),
                  evidence_changes=sum(c['evidence_changes'] for c in selected))
    for model in ('completed_archive', 'direction', 'timing_gated'):
        forecasts = frame.assign(probability_up=frame[model].astype(float))
        result[model] = dict(_metrics(forecasts, symbols), paired_to_completed_archive=_paired_hits(forecasts, baseline))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--archives', type=Path)
    mode.add_argument('--replay', type=Path)
    parser.add_argument('--scratch', type=Path)
    parser.add_argument('--bars', type=Path)
    parser.add_argument('--protocol-sha256', required=True)
    parser.add_argument('--sessions', required=True, type=date.fromisoformat, nargs='+')
    args = parser.parse_args()
    os.umask(0o077)
    protocol = rule.load(args.bundle)
    if rule.digest((args.bundle/'protocol.json').read_bytes()) != args.protocol_sha256:
        parser.error('Wrong pinned protocol')
    if sorted(set(args.sessions)) != args.sessions:
        parser.error('Dates must be unique and chronological')
    if args.replay:
        if not args.bars or len(args.sessions) < 2:
            parser.error('Scoring requires --bars and the trading date immediately before the first replay')
        if __package__:
            from .audit_opening_receipts import read_bars
        else:
            from audit_opening_receipts import read_bars
        replay = json.loads(args.replay.read_text())
        if (replay['evidence_kind'] != RECIPE or replay['protocol'] != args.protocol_sha256
                or replay['sessions'] != [str(d) for d in args.sessions[1:]]):
            parser.error('Wrong replay, frozen protocol or dates')
        prices = {d: read_bars(args.bars/f'{d:%Y%m%d}.json', d) for d in args.sessions}
        reports = [score(args.sessions, protocol['tickers'], prices, replay['cases'], phase, reserve)
                   for phase in (50_000, 150_000) for reserve in (0, 1_000_000)]
        print(json.dumps(dict(evidence_kind=RECIPE, timely_issued_sessions=0, scores=reports), allow_nan=False))
        return
    if not args.scratch:
        parser.error('Provide --scratch on the same filesystem as the archives')
    cases = []
    missing = []
    for day in args.sessions:
        path = args.archives / f'{day}.sqlite'
        if not path.is_file():
            missing.append(str(day))
            continue
        for index, phase in enumerate((50_000, 150_000)):
            for reserve in ((0,1_000_000) if (day.day + index) % 2 else (1_000_000,0)):
                cases.append(run(path, args.scratch.resolve(), day, protocol['tickers'], reserve, phase))
    print(json.dumps(dict(evidence_kind=RECIPE, timely_issued_sessions=0, protocol=args.protocol_sha256,
                          sessions=[str(d) for d in args.sessions], missing_archives=missing, cases=cases)))


if __name__ == '__main__':
    main()
