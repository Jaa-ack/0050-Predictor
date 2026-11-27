from datetime import date, timedelta
import sqlite3

import pytest

from scripts import opening_delivery as delivery
from scripts import opening_trial_rule as rule
from twquant.opening_capture import SCHEMA


def fixture(tmp_path):
    days = []
    day = date.today() + timedelta(days=3)
    while len(days) < 126:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    members = [str(t) for t in range(2000, 2050)]
    bundle = tmp_path / 'bundle'
    sha = rule.freeze(bundle, members, days)
    day = date.fromisoformat(days[0])
    archive = tmp_path / f'{day}.sqlite'
    with sqlite3.connect(archive) as db:
        db.executescript(SCHEMA)
    due = rule.clock_us(day, rule.DECISION) * 1000
    return bundle, sha, day, due


def test_original_receipt_wins_and_late_delivery_keeps_every_prediction(tmp_path, monkeypatch):
    bundle, sha, day, due = fixture(tmp_path)
    ticks = iter([due - 1000000000, due, due + 50000, due + 120000000])
    monkeypatch.setattr(delivery, 'now_ns', lambda: next(ticks))
    ledger = tmp_path / 'deliveries.sqlite'
    result = delivery.issue(bundle, sha, tmp_path, ledger)
    assert result['session'] == str(day)
    assert result['observed_ns'] == due + 120000000
    assert len(result['rows']) == sum(r['fallback'] for r in result['rows']) == 50
    monkeypatch.setattr(delivery, 'now_ns', lambda: due + 3600000000000)
    assert delivery.issue(bundle, sha, tmp_path, ledger) == result
    assert delivery.read_day(ledger, sha, str(day)) == result


def test_late_start_cannot_backfill_a_live_receipt(tmp_path, monkeypatch):
    bundle, sha, day, due = fixture(tmp_path)
    monkeypatch.setattr(delivery, 'now_ns', lambda: due + 1)
    ledger = tmp_path / 'deliveries.sqlite'
    with pytest.raises(ValueError, match='before cutoff'):
        delivery.issue(bundle, sha, tmp_path, ledger)
    assert delivery.read_day(ledger, sha, str(day)) is None


def test_scoring_keeps_late_missing_and_unknown_in_due_denominator(tmp_path):
    from scripts import audit_opening_delivery as audit
    bundle, sha, day, cutoff = fixture(tmp_path)
    protocol = rule.load(bundle)
    dates = [date.fromisoformat(x) for x in protocol['sessions'][:3]]
    prior = day - timedelta(days=1)
    prices = {d: {t: (101., 100.) for t in protocol['tickers']} for d in [prior, *dates]}
    prices[dates[0]].pop(protocol['tickers'][0])
    batches = {}
    for d, delay in zip(dates[:2], [0, 120000000], strict=True):
        due = rule.clock_us(d, rule.DECISION) * 1000
        rows = [dict(ticker=t, up=True, fallback=False, reference_received_us=due//1000-10,
                     trial_exchange_us=due//1000-3, trial_received_us=due//1000-1) for t in protocol['tickers']]
        batches[str(d)] = dict(session=str(d), protocol=sha, evidence_kind='private_local_delivery',
                               cutoff_ns=due, armed_ns=due-1000000000, sent_ns=due, observed_ns=due+delay,
                               rows=rows)
    result = audit.assess(protocol, sha, prior, prices, batches, rule.clock_us(dates[-1], rule.DECISION)*1000 + 10**9)
    assert result['scheduled_sessions'] == 126
    assert result['due_sessions'] == 3 and result['timely_sessions'] == 1
    assert result['late_sessions'] == [str(dates[1])]
    assert result['missing_sessions'] == [str(dates[2])]
    assert result['score']['expected'] == 150 and result['score']['correct'] == 49
    assert result['score']['coverage'] == 1 / 3
    assert result['score']['minimum_stock_accuracy'] == 0
    assert result['paired_to_not_up'] == dict(rescued=49, added_errors=0)
    assert result['accepted'] is False


def test_recovery_keeps_original_batch_and_observes_after_commit(tmp_path, monkeypatch):
    bundle, sha, day, due = fixture(tmp_path)
    ticks = iter([due-1000, due, due+10000])
    monkeypatch.setattr(delivery, 'now_ns', lambda: next(ticks))
    ledger = tmp_path / 'deliveries.sqlite'
    with pytest.raises(StopIteration):
        delivery.issue(bundle, sha, tmp_path, ledger)
    assert delivery.read_day(ledger, sha, str(day)) is None
    monkeypatch.setattr(delivery, 'now_ns', lambda: due + 3600000000000)
    result = delivery.issue(bundle, sha, tmp_path, ledger)
    assert result['sent_ns'] == due+10000
    assert result['observed_ns'] == due+3600000000000
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT count(*) FROM batches').fetchone() == (1,)


def test_inbox_rejects_different_pin_before_touching_ledger(tmp_path):
    bundle, sha, day, due = fixture(tmp_path)
    ledger = tmp_path / 'deliveries.sqlite'
    with pytest.raises(ValueError, match='pinned protocol'):
        delivery.issue(bundle, '0'*64, tmp_path, ledger)
    assert not ledger.exists()
