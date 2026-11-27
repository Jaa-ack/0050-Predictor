"""Deliver the frozen 08:59 batch to a private local inbox and record its actual visibility."""
import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time

if __package__:
    from . import opening_trial_rule as rule
else:
    import opening_trial_rule as rule

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 protocol TEXT NOT NULL, session TEXT NOT NULL, body BLOB NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY (protocol, session)
);
CREATE TABLE IF NOT EXISTS receipts (
 protocol TEXT NOT NULL, session TEXT NOT NULL, fingerprint TEXT NOT NULL, observed_ns INTEGER NOT NULL,
 PRIMARY KEY (protocol, session)
);
"""


def now_ns():
    return time.time_ns()


def _open(ledger):
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(ledger, os.O_CREAT | os.O_RDWR, 0o600))
    db = sqlite3.connect(ledger)
    db.execute('PRAGMA synchronous=FULL')
    db.executescript(SCHEMA)
    return db


def _batch(db, sha, day):
    row = db.execute('SELECT body, fingerprint FROM batches WHERE protocol=? AND session=?',
                     (sha, day)).fetchone()
    if row is None:
        return None
    body, fingerprint = row
    if rule.digest(body) != fingerprint:
        raise ValueError('Batch digest mismatch')
    batch = json.loads(body)
    if batch['protocol'] != sha or batch['session'] != day or batch['evidence_kind'] != 'private_local_delivery':
        raise ValueError('Unexpected batch identity')
    return batch, fingerprint


def _receipt(db, sha, day, fingerprint):
    row = db.execute('SELECT fingerprint, observed_ns FROM receipts WHERE protocol=? AND session=?',
                     (sha, day)).fetchone()
    if row is None:
        return None
    if row[0] != fingerprint:
        raise ValueError('Receipt digest mismatch')
    return row[1]


def read_day(ledger, sha, day):
    if not ledger.is_file():
        return None
    with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        stored = _batch(db, sha, day)
        if stored is None:
            return None
        batch, fingerprint = stored
        observed = _receipt(db, sha, day, fingerprint)
        return dict(batch, observed_ns=observed) if observed is not None else None


def _acknowledge(ledger, sha, day):
    # A separate connection must see the complete, committed payload before taking its clock reading.
    with closing(_open(ledger)) as receiver:
        batch, fingerprint = _batch(receiver, sha, day)
        observed = _receipt(receiver, sha, day, fingerprint)
        if observed is None:
            observed = now_ns()
            if observed < batch['sent_ns']:
                raise ValueError('Receipt clock moved backwards')
            with receiver:
                receiver.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)', (sha, day, fingerprint, observed))
            observed = _receipt(receiver, sha, day, fingerprint)
        return dict(batch, observed_ns=observed)


def _wait_for(cutoff):
    while True:
        remaining = cutoff - now_ns()
        if remaining <= 0:
            return
        time.sleep(remaining / 1e9)


def issue(bundle, sha, archives, ledger):
    protocol = rule.load(bundle)
    if rule.digest((bundle / 'protocol.json').read_bytes()) != sha:
        raise ValueError('Wrong pinned protocol')
    armed = now_ns()
    day = datetime.fromtimestamp(armed // 10**9, rule.LOCAL).date()
    iso = day.isoformat()
    if iso not in protocol['sessions']:
        raise ValueError('Today is outside the frozen calendar')
    cutoff = rule.clock_us(day, rule.DECISION) * 1000
    with closing(_open(ledger)) as inbox:
        if _batch(inbox, sha, iso) is None:
            if not cutoff - 60_000_000_000 <= armed < cutoff:
                raise ValueError('Arm during the final minute before cutoff')
            _wait_for(cutoff)
            path = archives.resolve() / f'{iso}.sqlite'
            if path.is_file():
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as archive:
                    rows = rule.directions(archive, day, protocol['tickers'])
                source = 'capture_archive'
            else:
                rows = [dict(ticker=s, up=False, fallback=True, reference_received_us=None,
                             trial_exchange_us=None, trial_received_us=None) for s in protocol['tickers']]
                source = 'missing_archive'
            sent = now_ns()
            if sent < cutoff:
                raise ValueError('Publication clock moved backwards')
            batch = dict(evidence_kind='private_local_delivery', protocol=sha, session=iso, cutoff_ns=cutoff,
                         armed_ns=armed, sent_ns=sent, source=source, rows=rows)
            body = json.dumps(batch, allow_nan=False, separators=(',', ':')).encode()
            with inbox:
                inbox.execute('INSERT OR IGNORE INTO batches VALUES (?,?,?,?)', (sha, iso, body, rule.digest(body)))
    return _acknowledge(ledger, sha, iso)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('bundle', 'archives', 'ledger'):
        parser.add_argument('--' + key, required=True, type=Path)
    parser.add_argument('--protocol-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    received = issue(args.bundle, args.protocol_sha256, args.archives, args.ledger)
    print(json.dumps(dict(session=received['session'], expected=len(received['rows']),
                          observed_ns=received['observed_ns'], delay_ns=received['observed_ns'] - received['cutoff_ns'])))


if __name__ == '__main__':
    main()
