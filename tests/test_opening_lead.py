from datetime import date
import sqlite3

from scripts import audit_opening_lead as lead
from scripts import opening_trial_rule as rule
from twquant.opening_capture import SCHEMA


def test_lead_censors_receipts_but_preserves_original_trial_window(tmp_path):
    day = date(2026, 11, 23)
    due = rule.clock_us(day, rule.DECISION)
    path = tmp_path / f'{day}.sqlite'
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    for s in ['2330', '2454', '2317']:
        db.execute("INSERT INTO quotes VALUES (?,'websocket',?,100,NULL,NULL,?)", (s, due-50_000_000, s))
    db.execute("INSERT INTO quotes VALUES ('2317','websocket',?,110,NULL,NULL,'newref')", (due-250_000,))
    for i,(s,exchange,receipt,price) in enumerate([
        ('2330',-2_000_000,-1_500_000,101),('2330',-1_300_000,-1_000_000,98),
        ('2454',-30_500_000,-30_100_000,105),('2317',-3_000_000,-2_500_000,105)]):
        db.execute("INSERT INTO trials (symbol,source,exchange_us,received_us,serial,price,book,digest) VALUES (?,?,?,?,?,?,?,?)",
                   (s,'websocket',due+exchange,due+receipt,i,price,'[]',str(i)))
    db.commit()
    db.close()
    rows = lead.compare_day(path, day, ['2330','2454','2317'])
    assert [(r['direct'], r['one_second_early']) for r in rows] == [(False,True),(False,False),(False,True)]
    assert rows[1]['early_fallback'] and rows[1]['direct_fallback']


def test_missing_archive_never_generates_correct_not_up_guesses(tmp_path):
    day = date(2026, 11, 23)
    rows = lead.compare_day(tmp_path / 'absent.sqlite', day, ['2330'])
    assert rows[0]['direct'] is None and rows[0]['one_second_early'] is None


def test_paired_scores_charge_missing_predictions_and_unknown_raw_labels():
    from twquant.universe import TRAINING_TICKERS
    symbols = [*TRAINING_TICKERS, '2002']
    prior, day = date(2026,11,20), date(2026,11,23)
    prices = {prior: {s:(100., 100.) for s in symbols}, day:{s:(100., 100.) for s in symbols[:-1]}}
    rows = [dict(ticker=s, direct=True, one_second_early=False, early_fallback=False, direct_fallback=False)
            for s in symbols[1:]]
    report = lead.score([prior,day], symbols, {str(day):rows}, prices)['scopes']
    assert report['fixed_ten']['early']['correct'] == 9
    assert report['constituents']['early']['expected'] == 11
    assert report['constituents']['early']['answered'] == 10
    assert report['constituents']['early']['known'] == 10
    assert report['constituents']['paired'] == {'rescued':9, 'added_errors':0}
