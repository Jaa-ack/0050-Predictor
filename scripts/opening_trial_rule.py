"""Freeze and run the direct 08:59 trial rule using the Python standard library."""
import argparse
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo('Asia/Taipei')
DECISION = time(8, 59)


def clock_us(day, clock):
    return int(datetime.combine(day, clock, LOCAL).timestamp()) * 1_000_000


def directions(db, day, symbols):
    """Read the two predecision inputs in one snapshot, without querying an opening label."""
    cutoff = clock_us(day, DECISION)
    start = clock_us(day, time(8, 58, 30))
    rows = []
    db.execute('BEGIN')
    try:
        for ticker in symbols:
            reference = db.execute(
                """SELECT previous_close, received_us FROM quotes WHERE symbol=? AND source='websocket'
                   AND previous_close>0 AND received_us<? ORDER BY received_us DESC LIMIT 1""",
                (ticker, cutoff)).fetchone()
            trial = db.execute(
                """SELECT price, exchange_us, received_us FROM trials WHERE symbol=? AND source='websocket'
                   AND price>0 AND exchange_us>=? AND exchange_us<=received_us AND received_us<?
                   ORDER BY exchange_us DESC, serial DESC, received_us LIMIT 1""",
                (ticker, start, cutoff)).fetchone()
            observed = trial is not None and reference is not None
            rows.append(dict(ticker=ticker, up=bool(observed and trial[0] > reference[0]), fallback=not observed,
                             reference_received_us=reference[1] if reference else None,
                             trial_exchange_us=trial[1] if trial else None, trial_received_us=trial[2] if trial else None))
    finally:
        db.rollback()
    return rows


def digest(data):
    return hashlib.sha256(data).hexdigest()


def freeze(output, symbols, sessions):
    if output.exists():
        raise FileExistsError(output)
    now = datetime.now(timezone.utc)
    days = [date.fromisoformat(d) for d in sessions]
    if (len(days) < 126 or days != sorted(set(days)) or days[0] <= now.astimezone(LOCAL).date()
            or any(d.weekday() >= 5 for d in days)):
        raise ValueError('Expected at least 126 ordered future exchange sessions')
    if len(symbols) != 50 or len(set(symbols)) != 50 or not all(re.fullmatch(r'[0-9]{4}', s) for s in symbols):
        raise ValueError('Expected 50 distinct constituents')
    source = Path(__file__).read_bytes()
    protocol = dict(model='direct_trial_v1', frozen_at=now.isoformat(), sessions=sessions, tickers=symbols,
                    task='0859_open_above_previous_session_close', local_cutoff='08:59:00 Asia/Taipei',
                    fallback='not_up', tie='not_up', label='official_open_above_previous_session_raw_close',
                    runner_sha256=digest(source))
    body = json.dumps(protocol, indent=2, allow_nan=False).encode()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / 'bundle'
        stage.mkdir(mode=0o700)
        for name, content in [('rule.py', source), ('protocol.json', body), ('protocol.sha256', digest(body).encode())]:
            dest = stage / name
            dest.write_bytes(content)
            dest.chmod(0o400)
        stage.rename(output)
    return digest(body)


def load(root):
    body = (root / 'protocol.json').read_bytes()
    if digest(body) != (root / 'protocol.sha256').read_text():
        raise ValueError('Frozen protocol digest mismatch')
    protocol = json.loads(body)
    if (digest((root / 'rule.py').read_bytes()) != protocol['runner_sha256']
            or digest(Path(__file__).read_bytes()) != protocol['runner_sha256']):
        raise ValueError('Use the matching frozen runner')
    return protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    freezer = sub.add_parser('freeze')
    for key in ('out', 'membership', 'sessions'):
        freezer.add_argument('--' + key, type=Path, required=True)
    replay = sub.add_parser('replay')
    replay.add_argument('--bundle', type=Path, required=True)
    replay.add_argument('--archives', type=Path, required=True)
    replay.add_argument('--session', type=date.fromisoformat, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == 'freeze':
        # Use the repository's verified metadata schema at freeze time only.
        from twquant.direction_audit import load_membership_snapshot
        members = load_membership_snapshot(args.membership)['tickers']
        print(freeze(args.out, members, json.loads(args.sessions.read_text())))
        return
    protocol = load(args.bundle)
    if args.session.isoformat() not in protocol['sessions']:
        parser.error('Session outside frozen calendar')
    path = args.archives.resolve() / f'{args.session}.sqlite'
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = directions(db, args.session, protocol['tickers'])
    finally:
        db.close()
    print(json.dumps(dict(evidence_kind='historical_receipt_replay', session=str(args.session), rows=rows)))


if __name__ == '__main__':
    main()
