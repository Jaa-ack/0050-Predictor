from datetime import date, time
import json
import sqlite3

import pytest

from scripts import opening_trial_rule as rule
from twquant.opening_capture import session_us, SCHEMA


def test_frozen_estimator_reads_only_causal_trial_and_reference_fields():
    day = date(2026, 11, 26)
    cutoff = session_us(day, time(8, 59))
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO quotes VALUES ('2330','websocket',?,100,NULL,NULL,'r')", (cutoff - 10,))
    db.execute("INSERT INTO quotes VALUES ('2330','websocket',?,200,NULL,NULL,'late')", (cutoff,))
    for i, (exchange, receipt, price) in enumerate([(-20, -15, 101.), (-14, 0, 99.), (-25, -3, 99.)]):
        db.execute("INSERT INTO trials (symbol,source,exchange_us,received_us,serial,price,book,digest) VALUES (?,?,?,?,?,?,?,?)",
                   ('2330', 'websocket', cutoff + exchange, cutoff + receipt, i, price, '[]', str(i)))
    db.commit()
    rows = rule.directions(db, day, ['2330', '2454'])
    assert [(r['ticker'], r['up'], r['fallback']) for r in rows] == [('2330', True, False), ('2454', False, True)]
    assert rows[0]['trial_received_us'] == cutoff - 15


def test_freeze_pins_calendar_members_and_executable_and_rejects_overwrite(tmp_path):
    from datetime import timedelta
    now = date.today()
    days = []
    while len(days) < 126:
        now += timedelta(days=1)
        if now.weekday() < 5:
            days.append(now.isoformat())
    members = [str(t) for t in range(2000, 2050)]
    out = tmp_path / 'frozen'
    rule.freeze(out, members, days)
    protocol = rule.load(out)
    assert protocol['sessions'] == days and protocol['tickers'] == members
    with pytest.raises(FileExistsError):
        rule.freeze(out, members, days)
    doc = json.loads((out / 'protocol.json').read_text())
    doc['tickers'].reverse()
    (out / 'protocol.json').chmod(0o600)
    (out / 'protocol.json').write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='digest'):
        rule.load(out)
