from datetime import date
import sqlite3

from scripts import opening_trial_rule as rule, rehearse_opening_delivery as replay
from twquant.opening_capture import SCHEMA


def test_replay_seeds_only_committed_history_and_releases_tail_by_receipt(tmp_path):
    day = date(2026, 11, 23)
    cutoff = rule.clock_us(day, rule.DECISION)
    source = tmp_path / 'history.sqlite'
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO quotes VALUES ('2330','websocket',?,100,NULL,NULL,'ref')", (cutoff-5_000_000,))
        for i, (source_kind, lag, price) in enumerate([
                ('websocket',-3_000_000,90), ('websocket',-1_250_000,110),
                ('websocket',-999_999,90), ('rest',-1_200_000,130), ('websocket',400_000,150)]):
            db.execute("INSERT INTO trials VALUES ('2330',?,?,?,?,?,NULL,NULL,NULL,'[]',?)",
                       (source_kind,cutoff+lag,cutoff+lag,i,price,str(i)))
    scratch = tmp_path / 'scratch.sqlite'
    tail = replay.prepare(source, scratch, cutoff)
    with sqlite3.connect(scratch) as writer:
        assert rule.directions(writer,day,['2330'])[0]['up'] is False
        assert len(tail) == 2
        used = replay.flush(writer, tail, 0, cutoff-1_000_000)
        assert used == 1
        assert rule.directions(writer,day,['2330'])[0]['up'] is True
        assert replay.flush(writer, tail, used, cutoff) == 2
        assert rule.directions(writer,day,['2330'])[0]['up'] is False
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT count(*) FROM trials').fetchone()[0] == 5


def test_timely_rehearsal_scoring_charges_whole_late_or_missing_batch():
    days = [date(2026, 11, d) for d in (20,23,24,25)]
    tickers = ['2330','2454']
    prices = {d:{s:(101.,100.) for s in tickers} for d in days}
    rows = [dict(ticker=s, direct=True, replayed=True) for s in tickers]
    cases = [dict(session=str(d),phase_us=50_000,reserve_us=1_000_000,receipt_delay_ns=lag,
                  observed=rows, evidence_changes=0) for d,lag in zip(days[1:3],[-1,1],strict=True)]
    result = replay.score(days,tickers,prices,cases,50_000,1_000_000)
    assert result['timely_rereads'] == 1
    assert result['missing_days'] == [str(days[3])]
    assert result['timing_gated']['expected'] == 6
    assert result['timing_gated']['answered'] == result['timing_gated']['correct'] == 2
    assert result['timing_gated']['paired_to_completed_archive'] == dict(rescued=0, added_errors=2)
    assert result['direction']['answered'] == 4
