from datetime import date
import sqlite3

from scripts import opening_trial_rule as rule, research_opening_trial_path as study
from twquant.opening_capture import SCHEMA


def test_paths_use_three_distinct_exchange_updates_before_deadline(tmp_path):
    day = date(2026, 11, 23)
    cutoff = rule.clock_us(day, rule.DECISION)
    path = tmp_path / 'capture.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO quotes VALUES ('2330','websocket',?,100,NULL,NULL,'ref')", (cutoff-500,))
        db.execute("INSERT INTO quotes VALUES ('2330','websocket',?,110,NULL,NULL,'late')", (cutoff,))
        for i, (exchange, receipt, price) in enumerate([
            (-8,-7,101),(-6,-5,102),(-4,-3,100),(-4,-2,100),(-1,0,90),(-1,-2,110)]):
            db.execute("INSERT INTO trials VALUES ('2330','websocket',?,?,?,?,NULL,NULL,NULL,'[]',?)",
                       (cutoff+exchange, cutoff+receipt, i, price, str(i)))
    rows = study.paths(path, day, ['2330', '2454'])
    assert rows == {'2330': [0, 1, 1], '2454': []}
    assert study.forecasts(rows['2330']) == (False, True, True)
    assert study.forecasts([1,-1,0]) == (True, False, True)
    assert study.forecasts([0,1]) == (False, False, False)
    assert study.forecasts([]) == (False, False, False)
    assert study.paths(tmp_path/'absent', day, ['2330']) == {}
    assert study.forecasts(None) == (None, None, None)


def test_selection_uses_only_prior_mature_fixed_ten_labels_and_counts_absent_rows():
    from twquant.universe import TRAINING_TICKERS
    core = list(TRAINING_TICKERS)
    symbols = [*core, '2002']
    days = [date(2026, 11, d) for d in (20, 23, 24)]
    paths = {s:[0,1,1] for s in symbols}
    inputs = dict(held_out=str(days[-1]), observed={str(days[1]):paths},
                  calibration_labels={str(days[1]):dict.fromkeys(core, True)})
    prices = {d:{s:(90.,100.) for s in symbols} for d in days}
    result = study.score(days, symbols, inputs, prices)
    assert result['selected'] == 'majority'
    assert result['calibration']['majority']['correct'] == 10
    assert result['holdout']['majority']['expected'] == len(symbols)
    assert result['holdout']['majority']['correct'] == result['holdout']['majority']['answered'] == 0
    assert result['missing_archives'] == [str(days[-1])]
    inputs['calibration_labels'] = {str(days[1]):{'2002': True}, str(days[-1]):dict.fromkeys(core, True)}
    result = study.score(days, symbols, inputs, prices)
    assert result['selected'] == 'direct'
    assert result['calibration']['majority']['known'] == 0


def test_collect_excludes_labels_first_received_at_holdout_cutoff(tmp_path):
    days = [date(2026, 11, d) for d in (23, 24)]
    cutoff = rule.clock_us(days[-1], rule.DECISION)
    with sqlite3.connect(tmp_path / f'{days[0]}.sqlite') as db:
        db.executescript(SCHEMA)
        for symbol, received in [('2330',cutoff-1), ('2454',cutoff)]:
            db.execute("INSERT INTO quotes VALUES (?,'rest',?,100,101,?,?)",
                       (symbol, received, rule.clock_us(days[0], study.time(9)), symbol))
    data = study.collect(tmp_path, days, ['2330','2454','2002'], ['2330','2454'])
    assert data['calibration_labels'] == {str(days[0]):{'2330':True, '2454':None}}
    assert data['observed'][str(days[1])] == {}
