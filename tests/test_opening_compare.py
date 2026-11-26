from datetime import date, datetime, time
import json
from math import isclose

from twquant.opening_capture import Archive, COLLECTION_DAYS, TZ, evaluate_week, session_us
from twquant import opening_compare
from twquant.universe import TRAINING_TICKERS


def add(store, ticker, trial, opened, *, label_day=None, source='websocket'):
    day = store.day
    stamp = session_us(day, time(8, 58, 45))
    if trial is not None:
        store.aggregate(dict(date=day.isoformat(), symbol=ticker, previousClose=100, isTrial=True,
                             lastTrial=dict(time=stamp, price=trial, size=20, serial=1)),
                        stamp + 100)
    if opened is not None:
        store.quote(dict(date=day.isoformat(), symbol=ticker, previousClose=100, openPrice=opened,
                         openTime=session_us(day, time(9))),
                    session_us(label_day or day, time(13, 40)), source)


def test_comparison_fits_only_mature_fixed_ten_and_scores_complete_holdout(tmp_path):
    days = COLLECTION_DAYS[:3]
    symbols = (*TRAINING_TICKERS, '1216', '2412')
    for day in days[:-1]:
        with Archive(tmp_path, day) as store:
            for i, ticker in enumerate(TRAINING_TICKERS):
                add(store, ticker, 98 if i % 2 else 102, 99 if i % 2 else 103)
            add(store, '1216', 102, 175)
            add(store, '2412', 102, 175, label_day=days[-1], source='rest')
            add(store, TRAINING_TICKERS[0], None, 80, label_day=days[-1], source='rest')
    with Archive(tmp_path, days[-1]) as store:
        for ticker in TRAINING_TICKERS:
            add(store, ticker, 99.5, 100.5)
        add(store, '1216', None, 99)
    result = opening_compare.compare(tmp_path, symbols, days)
    assert result['original']['train_rows'] == 20
    assert result['repair']['train_rows'] == 20
    assert isclose(result['repair']['correction'], .01)
    assert result['repair']['score']['correct'] == 11
    assert result['repair']['score']['expected'] == 12
    assert result['repair']['score']['unknown_answers'] == 1
    assert result['repair']['paired'] == {'rescued': 10, 'added_errors': 0}
    missing = result['repair']['rows'][-2]
    assert missing['fallback_reason'] == 'trial_missing_not_up'
    assert 'probability_up' not in missing


def test_corrected_tie_and_missing_training_keep_defined_direction(tmp_path):
    days = COLLECTION_DAYS[:3]
    ticker = TRAINING_TICKERS[0]
    with Archive(tmp_path, days[-1]) as store:
        add(store, ticker, 101, 100)
    report = opening_compare.compare(tmp_path, (ticker,), days)
    assert report['repair']['correction'] is None
    assert report['repair']['rows'][0]['predicted_up'] is True
    for day in days[:-1]:
        with Archive(tmp_path, day) as store:
            add(store, ticker, 101, 100)
    report = opening_compare.compare(tmp_path, (ticker,), days)
    assert isclose(report['repair']['correction'], -.01)
    assert report['repair']['rows'][0]['predicted_up'] is False


def test_future_capture_dates_are_explicit_and_do_not_change_fixed_week(tmp_path):
    calendar = tmp_path / 'sessions.json'
    calendar.write_text('["2026-11-30", "2026-12-02"]')
    days = opening_compare.read_future_sessions(calendar)
    assert days == (date(2026, 11, 30), date(2026, 12, 2))
    units = opening_compare.future_units(days)
    schedule = units['twquant-opening-future-collect.timer']
    assert '2026-11-30 08:28..59:00 Asia/Taipei' in schedule
    assert '2026-12-01' not in schedule
    assert COLLECTION_DAYS[-1] == date(2026, 11, 27)


def test_workspace_exposes_two_models_and_shared_trial_baseline(tmp_path):
    from twquant.opening_web import OpeningWorkspace

    days = COLLECTION_DAYS[:3]
    ticker = TRAINING_TICKERS[0]
    for day in days:
        with Archive(tmp_path, day) as store:
            add(store, ticker, 101, 100)
    report = opening_compare.compare(tmp_path, (ticker,), days)
    (tmp_path / f'comparison-{days[-1]}.json').write_text(json.dumps(report))
    workspace = OpeningWorkspace(None, tmp_path)
    summary = workspace.summary()
    assert summary['week']['date'] == days[-1].isoformat()
    assert summary['repair']['date'] == days[-1].isoformat()
    assert summary['paired_window']['sessions'] == 1
    assert summary['paired_window']['worst_five'] == {}
    score = summary['comparisons'][days[-1].isoformat()]
    assert score['trial']['correct'] == 0
    assert score['repair']['correct'] == 1
    assert score['repair_paired']['rescued'] == 1
    new_row, = workspace.predictions('repair', days[-1])['rows']
    assert new_row['trial_up'] is True
    assert new_row['predicted_up'] is False
    assert new_row['correct'] is True
    assert new_row['probability'] is None
    assert workspace.predictions('week', days[-1])['rows'][0]['ticker'] == ticker


def test_final_comparison_reads_official_report_and_preserves_it(tmp_path, monkeypatch):
    symbols = (*TRAINING_TICKERS, *(str(number) for number in range(1000, 1040)))
    days = COLLECTION_DAYS
    (tmp_path / 'symbols.json').write_text(json.dumps(symbols))
    for day in days:
        with Archive(tmp_path, day) as store:
            for i, ticker in enumerate(symbols):
                add(store, ticker, 101 if i % 2 else 99, 101 if i % 2 else 99)
    official = tmp_path / 'evaluation.json'
    original = json.dumps(evaluate_week(tmp_path, symbols))
    official.write_text(original)
    monkeypatch.setattr(opening_compare.capture, '_now', lambda: datetime.combine(days[-1], time(14, 32), TZ))
    assert opening_compare.main(['compare', '--root', str(tmp_path)]) == 0
    assert official.read_text() == original
    report = json.loads((tmp_path / f'comparison-{days[-1]}.json').read_text())
    assert report['original'] == json.loads(original)
    assert report['repair']['train_rows'] == 40
    assert report['repair']['score']['expected'] == 50


def test_ascending_supplement_stops_at_window_end_and_keeps_late_receipt(tmp_path):
    day = COLLECTION_DAYS[0]
    ticker = TRAINING_TICKERS[0]
    event = session_us(day, time(8, 58, 45))
    receipt = session_us(day, time(13, 41))
    calls = []

    def request(**parameters):
        calls.append(parameters)
        rows = [dict(time=event, price=101, serial=1)]
        rows += [dict(time=session_us(day, time(9, 10)) + i, price=101, serial=i + 2) for i in range(499)]
        return dict(date=day.isoformat(), symbol=ticker, data=rows), receipt

    with Archive(tmp_path, day) as store:
        opening_compare._ascending_trials(store, ticker, request)
        assert calls == [dict(symbol=ticker, isTrial=True, limit=500, offset=0, sort='asc')]
        assert store.conn.execute("SELECT source,exchange_us,received_us FROM trials").fetchall() == [('rest', event, receipt)]
        assert store.panel((ticker,))[0]['trial_gap'] is None


def test_recovery_falls_back_with_the_existing_pilot_calendar(tmp_path, monkeypatch):
    attempted = []
    monkeypatch.setattr(opening_compare.capture, 'main', lambda argv: 1)

    def fallback(root, days):
        attempted.append((root, days))
        return 0

    monkeypatch.setattr(opening_compare, '_supplement', fallback)
    assert opening_compare.main(['recover', '--root', str(tmp_path)]) == 0
    assert attempted == [(tmp_path, COLLECTION_DAYS)]


def test_weakest_five_rank_stocks_across_paired_days_and_charge_unknown():
    from twquant.opening_web import _paired_window

    tickers = ('2330', '2454', '2317', '2308', '3711', '1303')
    reports = {}
    for day, missed in (('2026-11-25', '2330'), ('2026-11-26', '2454')):
        rows = [dict(ticker=ticker, actual_up=None if ticker == '1303' else True,
                     trial_up=True, predicted_up=True) for ticker in tickers]
        original = [dict(row, predicted_up=row['ticker'] != missed) for row in rows]
        reports[day] = dict(original=dict(validation_rows=original), repair=dict(rows=rows))
    assert _paired_window(reports)['worst_five'] == {'original': .6, 'trial': .8, 'repair': .8}
