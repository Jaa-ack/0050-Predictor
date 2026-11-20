"""Bounded read-only 08:59 auction collection and a chronological one-week check."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
import socket
import sqlite3
from threading import Event
import time as wall
from zoneinfo import ZoneInfo

from twquant.universe import TRAINING_TICKERS

TZ = ZoneInfo('Asia/Taipei')
COLLECTION_DAYS = tuple(date(2026, 11, day) for day in range(23, 28))
PREPARE = time(8, 28)
AUCTION_START = time(8, 30)
CUTOFF = time(8, 59)
WINDOW_START = time(8, 58, 30)
OPEN = time(9)
COLLECT_END = time(9, 10)
RECOVER_START = time(13, 35)
RECOVER_END = time(14, 5)
EVALUATE_AT = time(14, 10)
BASE = '/opt/twquant-pilot'
INSTALL = '/opt/twquant-opening'
FEATURES = ('trial_gap', 'log_matched_size', 'book_imbalance')
SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
 symbol TEXT NOT NULL, source TEXT NOT NULL, exchange_us INTEGER NOT NULL,
 received_us INTEGER NOT NULL, serial INTEGER NOT NULL, price REAL,
 size REAL, bid REAL, ask REAL, book TEXT NOT NULL, digest TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS trials_lookup ON trials(symbol, source, exchange_us);
CREATE TABLE IF NOT EXISTS quotes (
 symbol TEXT NOT NULL, source TEXT NOT NULL, received_us INTEGER NOT NULL,
 previous_close REAL, open_price REAL, open_us INTEGER, digest TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS quotes_lookup ON quotes(symbol, source, received_us);
CREATE TABLE IF NOT EXISTS operations (
 received_us INTEGER NOT NULL, name TEXT NOT NULL, symbol TEXT NOT NULL, value TEXT NOT NULL
);
"""


def session_us(day, clock):
    return int(datetime.combine(day, clock, tzinfo=TZ).timestamp() * 1_000_000)


def received_us():
    return wall.time_ns() // 1_000


def _number(value, *, allow_zero=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value if value > 0 or (allow_zero and value == 0) else None


def _timestamp(value):
    return value if type(value) is int and 0 < value < 10**16 else None


def _book(data):
    def levels(field):
        source = data.get(field)
        if not isinstance(source, list):
            return []
        return [[_number(item.get('price'), allow_zero=True), _number(item.get('size'), allow_zero=True)]
                for item in source[:5] if isinstance(item, dict)]

    return [levels('bids'), levels('asks')]


def _encoded(payload):
    return json.dumps(payload, separators=(',', ':'), allow_nan=False)


class Archive:
    def __init__(self, root, day, *, readonly=False):
        self.day = day
        self.path = root / f'{day.isoformat()}.sqlite'
        self.readonly = readonly

    def __enter__(self):
        if self.readonly:
            self.conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        else:
            self.conn = sqlite3.connect(self.path, timeout=10)
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.executescript(SCHEMA)
        return self

    def __exit__(self, *error):
        try:
            self.conn.__exit__(*error)
        finally:
            self.conn.close()

    def trial(self, symbol, observation, receipt, source, *, book=None):
        exchange = _timestamp(observation.get('time'))
        if exchange is None or not session_us(self.day, AUCTION_START) <= exchange < session_us(self.day, COLLECT_END):
            return
        serial = observation.get('serial')
        payload = (symbol, source, exchange, serial if type(serial) is int else -1,
                   _number(observation.get('price')), _number(observation.get('size'), allow_zero=True),
                   _number(observation.get('bid')), _number(observation.get('ask')), _encoded(book or []))
        digest = hashlib.sha256(_encoded(payload).encode()).hexdigest()
        self.conn.execute('INSERT OR IGNORE INTO trials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          (*payload[:3], receipt, *payload[3:], digest))

    def quote(self, data, receipt, source):
        if data.get('date') != self.day.isoformat():
            return
        previous = _number(data.get('previousClose'))
        open_price, open_us = _number(data.get('openPrice')), _timestamp(data.get('openTime'))
        if data.get('isTrial') or open_us is None or not session_us(self.day, OPEN) <= open_us <= receipt:
            open_price, open_us = None, None
        if previous is None and open_price is None:
            return
        payload = (data['symbol'], source, previous, open_price, open_us)
        digest = hashlib.sha256(_encoded(payload).encode()).hexdigest()
        self.conn.execute('INSERT OR IGNORE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)',
                          (*payload[:2], receipt, *payload[2:], digest))

    def aggregate(self, data, receipt):
        if data.get('date') != self.day.isoformat():
            return
        self.quote(data, receipt, 'websocket')
        observation = data.get('lastTrial')
        if isinstance(observation, dict):
            self.trial(data['symbol'], observation, receipt, 'websocket',
                       book=_book(data) if data.get('isTrial') else None)

    def status(self, name, symbol='', value=''):
        self.conn.execute('INSERT INTO operations VALUES (?, ?, ?, ?)', (received_us(), name, symbol, value))
        self.conn.commit()

    def done(self, name, symbol):
        return self.conn.execute('SELECT 1 FROM operations WHERE name=? AND symbol=? LIMIT 1',
                                 (name, symbol)).fetchone() is not None

    def panel(self, symbols, *, label_before=None):
        cutoff = session_us(self.day, CUTOFF)
        window = session_us(self.day, WINDOW_START)
        rows = []
        for symbol in symbols:
            reference = self.conn.execute(
                """SELECT previous_close FROM quotes WHERE symbol=? AND source='websocket'
                   AND previous_close IS NOT NULL AND received_us<? ORDER BY received_us DESC LIMIT 1""",
                (symbol, cutoff)).fetchone()
            trial = self.conn.execute(
                """SELECT price, size, book FROM trials WHERE symbol=? AND source='websocket'
                   AND price IS NOT NULL AND exchange_us>=? AND exchange_us<=received_us AND received_us<?
                   ORDER BY exchange_us DESC, serial DESC, received_us LIMIT 1""",
                (symbol, window, cutoff)).fetchone()
            outcome = self.conn.execute(
                """SELECT previous_close, open_price FROM quotes WHERE symbol=? AND open_price IS NOT NULL
                   AND previous_close IS NOT NULL AND received_us<?
                   ORDER BY (source='rest') DESC, received_us DESC LIMIT 1""",
                (symbol, label_before if label_before is not None else 2**63 - 1)).fetchone()
            counts = dict(self.conn.execute('SELECT source, count(*) FROM trials WHERE symbol=? GROUP BY source', (symbol,)))
            row = dict(ticker=symbol, trial_gap=None, log_matched_size=None, book_imbalance=None,
                       actual_up=(outcome[1] > outcome[0]) if outcome else None, trial_receipts=counts)
            if trial is not None and reference is not None:
                row['trial_gap'] = trial[0] / reference[0] - 1
                if trial[1] is not None:
                    row['log_matched_size'] = math.log1p(trial[1])
                book = json.loads(trial[2])
                if book and all(side for side in book):
                    bid, ask = (sum(level[1] or 0 for level in side) for side in book)
                    if bid + ask:
                        row['book_imbalance'] = (bid - ask) / (bid + ask)
            rows.append(row)
        return rows


def _now():
    return datetime.now(TZ)


def _notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(b'READY=1', address)


def _collect(store, symbols):
    from fubon_neo.sdk import Mode
    from twquant.live_market import market_sdk

    stop, problem = Event(), Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    queue = Queue(maxsize=8192)
    acks = set()
    official_seen = set()
    universe = set(symbols)
    last_message = wall.monotonic()
    market_open = session_us(store.day, OPEN)
    reason = 'window_closed'

    def receive(raw):
        nonlocal last_message
        receipt = received_us()
        try:
            message = json.loads(raw)
            event, payload = message.get('event'), message.get('data')
            if event == 'subscribed':
                items = payload if isinstance(payload, list) else [payload]
                acks.update(item.get('symbol') for item in items
                            if isinstance(item, dict) and item.get('channel') == 'aggregates')
            elif event in {'data', 'snapshot'} and message.get('channel') == 'aggregates':
                if isinstance(payload, dict) and payload.get('symbol') in universe:
                    last_message = wall.monotonic()
                    ticker = payload['symbol']
                    if receipt >= market_open and ticker in official_seen:
                        return
                    queue.put((payload, receipt), timeout=.15)
                    opened = _timestamp(payload.get('openTime'))
                    if (payload.get('date') == store.day.isoformat() and not payload.get('isTrial')
                            and opened is not None and market_open <= opened <= receipt
                            and _number(payload.get('openPrice')) and _number(payload.get('previousClose'))):
                        official_seen.add(ticker)
            elif event == 'error':
                problem.set()
        except (ValueError, TypeError, Full):
            problem.set()

    sdk = market_sdk()
    sdk.init_realtime(Mode.Normal)
    stock = sdk.marketdata.websocket_client.stock
    stock.on('message', receive)
    stock.on('error', lambda *_: problem.set())
    stock.on('disconnect', lambda *_: problem.set())
    deadline = wall.monotonic() + 25
    end = session_us(store.day, COLLECT_END)
    recorded_ready = False
    entries = []
    store.status('collect_start')
    try:
        stock.connect()
        stock.subscribe({'channel': 'aggregates', 'symbols': list(symbols)})
        flush_at = wall.monotonic() + .2
        while received_us() < end and not stop.is_set():
            if problem.is_set():
                reason = 'stream_or_receipt_queue_failed'
                break
            if universe <= acks and not recorded_ready:
                store.status('full_subscription_ack', value=str(len(universe)))
                _notify_ready()
                recorded_ready = True
            if not recorded_ready and wall.monotonic() > deadline:
                reason = 'subscription_ack_timeout'
                break
            if _now().time() >= time(8, 34) and wall.monotonic() - last_message > 45:
                reason = 'stream_stale'
                break
            try:
                entries.append(queue.get(timeout=max(0., flush_at - wall.monotonic())))
            except Empty:
                pass
            if len(entries) >= 256 or wall.monotonic() >= flush_at:
                _persist(store, entries, end)
                entries = []
                flush_at = wall.monotonic() + .2
        if stop.is_set():
            reason = 'service_stopped'
    except Exception:
        reason = 'stream_connect_failed'
    finally:
        try:
            stock.disconnect()
        except Exception:
            pass
        remaining = entries
        while not queue.empty():
            remaining.append(queue.get_nowait())
        _persist(store, remaining, end)
        store.status('collect_end', value=reason)
    return reason in {'window_closed', 'service_stopped'}


def _persist(store, entries, end):
    for frame, receipt in entries:
        if receipt < end:
            store.aggregate(frame, receipt)
    if entries:
        store.conn.commit()


def _recover(store, symbols):
    from twquant.live_market import market_sdk

    missing = [(stage, ticker) for stage in ('open', 'trial') for ticker in symbols
               if not store.done(stage + '_rest_complete', ticker)]
    if not missing:
        return True
    sdk = market_sdk()
    endpoint = sdk.marketdata.rest_client.stock.intraday
    failed = False

    def request(stage, ticker, **parameters):
        wall.sleep(1.05)
        if _now().time() >= RECOVER_END:
            raise TimeoutError('recovery_window_closed')
        result = stage(symbol=ticker, **parameters)
        receipt = received_us()
        if result.get('date') != store.day.isoformat() or result.get('symbol') != ticker:
            raise ValueError('recovery_wrong_session')
        return result, receipt

    for stage, ticker in missing:
        if _now().time() >= RECOVER_END:
            return False
        try:
            if stage == 'open':
                quote, receipt = request(endpoint.quote, ticker)
                store.quote(quote, receipt, 'rest')
            else:
                for offset in range(0, 2_000, 500):
                    response, receipt = request(endpoint.trades, ticker, isTrial=True, limit=500, offset=offset)
                    rows = response['data']
                    if not isinstance(rows, list):
                        raise ValueError('unexpected_trial_schema')
                    for row in rows:
                        store.trial(ticker, row, receipt, 'rest')
                    store.conn.commit()
                    if len(rows) < 500:
                        break
                else:
                    raise ValueError('trial_pagination_incomplete')
            store.status(stage + '_rest_complete', ticker)
        except Exception:
            store.status(stage + '_rest_failed', ticker)
            failed = True
    return not failed


def _summarize(rows, predictions):
    correct = [row['actual_up'] is not None and row['actual_up'] == pred
               for row, pred in zip(rows, predictions, strict=True)]
    known = sum(row['actual_up'] is not None for row in rows)
    recall = {}
    for label, value in [('up', True), ('not_up', False)]:
        group = [hit for row, hit in zip(rows, correct, strict=True) if row['actual_up'] is value]
        recall[label] = sum(group) / len(group) if group else None
    return dict(expected=len(rows), correct=sum(correct), accuracy=sum(correct) / len(rows),
                unknown_answers=len(rows) - known, timed_trial_inputs=sum(row['trial_gap'] is not None for row in rows),
                recall=recall)


def evaluate_week(root, symbols):
    """Freeze a four-session training cut and score the final date with the complete denominator."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits

    days = {}
    for day in COLLECTION_DAYS:
        archive = Archive(root, day, readonly=True)
        if archive.path.exists():
            with archive as store:
                if store.conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('opening_archive_integrity_failed')
                horizon = session_us(COLLECTION_DAYS[-1], CUTOFF) if day < COLLECTION_DAYS[-1] else None
                days[day] = store.panel(symbols, label_before=horizon)
        else:
            days[day] = [dict(ticker=ticker, actual_up=None, trial_receipts={}, **dict.fromkeys(FEATURES))
                         for ticker in symbols]
    train = [row for day in COLLECTION_DAYS[:-1] for row in days[day]
             if row['ticker'] in TRAINING_TICKERS and row['actual_up'] is not None and row['trial_gap'] is not None]
    previous = COLLECTION_DAYS[:-1]
    fit_days = sum(any(row['ticker'] in TRAINING_TICKERS and row['actual_up'] is not None
                       and row['trial_gap'] is not None for row in days[day]) for day in previous)
    holdout = days[COLLECTION_DAYS[-1]]
    classes = [row['actual_up'] for row in train]
    prior_up = classes.count(True) * 2 > len(classes)
    predictions = [prior_up] * len(holdout)
    model_record = None
    if fit_days >= 2 and len(set(classes)) == 2:
        features = [name for name in FEATURES if any(row[name] is not None for row in train)]
        x = np.array([[row[name] for name in features] for row in train], dtype=float)
        median = np.nanmedian(x, axis=0)
        x = np.where(np.isnan(x), median, x)
        center = x.mean(axis=0)
        scale = x.std(axis=0)
        scale[scale == 0] = 1
        with threadpool_limits(limits=1):
            model = LogisticRegression(C=.1, random_state=42).fit((x - center) / scale, classes)
        for i, row in enumerate(holdout):
            if row['trial_gap'] is not None:
                values = np.array([row[name] for name in features], dtype=float)
                values = np.where(np.isnan(values), median, values)
                predictions[i] = bool(model.predict(((values - center) / scale).reshape(1, -1))[0])
        model_record = dict(features=features, median=median.tolist(), center=center.tolist(), scale=scale.tolist(),
                            coefficients=model.coef_[0].tolist(), intercept=float(model.intercept_[0]))
    observed = {day.isoformat(): dict(
        **_summarize(rows, [row['trial_gap'] is not None and row['trial_gap'] > 0 for row in rows]),
        websocket_trials=sum(row['trial_receipts'].get('websocket', 0) for row in rows),
        rest_trials=sum(row['trial_receipts'].get('rest', 0) for row in rows))
        for day, rows in days.items()}
    return dict(evidence_kind='short_chronological_capture_replay', task='0859_open_above_previous_session_close',
                training_tickers=list(TRAINING_TICKERS), fit_through=COLLECTION_DAYS[-2].isoformat(),
                validation_date=COLLECTION_DAYS[-1].isoformat(), train_rows=len(train), train_days=fit_days,
                real_world_target_verified=False, live_forecasts_issued=False, model=model_record,
                fallback='fixed_ten_training_majority' if train else 'not_up',
                validation=_summarize(holdout, predictions),
                validation_trial_rule=observed[COLLECTION_DAYS[-1].isoformat()],
                validation_fixed_not_up=_summarize(holdout, [False] * len(holdout)),
                validation_fixed_up=_summarize(holdout, [True] * len(holdout)), collection=observed,
                validation_rows=[dict(ticker=row['ticker'], actual_up=row['actual_up'], predicted_up=prediction,
                                      trial_available=row['trial_gap'] is not None)
                                 for row, prediction in zip(holdout, predictions, strict=True)])


def unit_files():
    calendars = {
        'collect': ('2026-11-23..27 08:28..59:00', '2026-11-23..27 09:00..09:00'),
        'recover': ('2026-11-23..27 13:35,45,55:00',),
        'evaluate': ('2026-11-27 14:10:00', '2026-11-27 14:25:00'),
    }
    units = {}
    for phase, dates in calendars.items():
        name = f'twquant-opening-{phase}'
        schedule = ''.join(f'OnCalendar={trigger} Asia/Taipei\n' for trigger in dates)
        runtime = {'collect': '44min', 'recover': '12min', 'evaluate': '4min'}[phase]
        startup = ('Type=notify\nNotifyAccess=main\nTimeoutStartSec=55s\n'
                   if phase == 'collect' else 'Type=exec\n')
        restart = ('Restart=on-failure\nRestartSec=25s\n' if phase != 'evaluate' else '')
        units[name + '.service'] = (
            f'[Unit]\nDescription=0050 opening auction {phase}\nAfter=network-online.target\n'
            'Wants=network-online.target\nStartLimitIntervalSec=0\n'
            f'[Service]\n{startup}User=twquant-pilot\nGroup=twquant-pilot\n'
            f'WorkingDirectory={BASE}\nEnvironment=PYTHONPATH={BASE}/app/src\n'
            'Environment=LOKY_MAX_CPU_COUNT=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1\n'
            f'ExecStart={BASE}/venv/bin/python {INSTALL}/capture.py {phase} --root {INSTALL}/runtime\n'
            f'RuntimeMaxSec={runtime}\nTimeoutStopSec=15s\n{restart}'
            'NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n'
            f'ReadWritePaths={BASE}/log {INSTALL}/runtime\nUMask=0077\n'
            'MemoryMax=384M\nNice=8\n')
        units[name + '.timer'] = (
            f'[Unit]\nDescription=Bounded 0050 opening auction {phase}\n[Timer]\n{schedule}'
            'Persistent=true\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n')
    return units


@contextmanager
def _lock(path):
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['collect', 'recover', 'evaluate', 'units'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args(argv)
    if args.mode == 'units':
        print(_encoded(unit_files()))
        return 0
    if args.root is None or not args.root.is_absolute():
        parser.error('An absolute private root is required')
    root = args.root
    os.umask(0o077)
    now = _now()
    day, clock = now.date(), now.time()
    if args.mode == 'collect' and (day not in COLLECTION_DAYS or not PREPARE <= clock < COLLECT_END):
        _notify_ready()
        return 0
    if args.mode == 'recover' and (day not in COLLECTION_DAYS or not RECOVER_START <= clock < RECOVER_END):
        return 0
    if args.mode == 'evaluate' and now < datetime.combine(COLLECTION_DAYS[-1], EVALUATE_AT, tzinfo=TZ):
        return 0
    try:
        symbols = tuple(json.loads((root / 'symbols.json').read_text()))
        if len(set(symbols)) != 50 or len(symbols) != 50 or not set(TRAINING_TICKERS) <= set(symbols) or not all(
                isinstance(ticker, str) and re.fullmatch(r'[0-9]{4}', ticker) for ticker in symbols):
            raise ValueError('invalid_fixed_constituent_set')
        with _lock(root / 'opening.lock') as locked:
            if not locked:
                return 1
            if args.mode == 'evaluate':
                output = root / 'evaluation.json'
                if not output.exists():
                    result = evaluate_week(root, symbols)
                    temporary = root / 'evaluation.tmp'
                    with temporary.open('w', encoding='utf-8') as file:
                        file.write(_encoded(result) + '\n')
                        file.flush()
                        os.fsync(file.fileno())
                    temporary.replace(output)
                return 0
            with Archive(root, day) as store:
                action = _collect if args.mode == 'collect' else _recover
                return 0 if action(store, symbols) else 1
    except Exception as exc:
        # Authentication and raw provider payloads never reach the service journal.
        print(f'opening_{args.mode}_failed:{type(exc).__name__}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
