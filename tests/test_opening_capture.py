from datetime import date, time

from twquant.opening_capture import Archive, COLLECTION_DAYS, evaluate_week, session_us, unit_files
from twquant.universe import TRAINING_TICKERS


def record(day, ticker, price, seconds):
    event = session_us(day, time(8, 58, seconds))
    return dict(date=day.isoformat(), symbol=ticker, previousClose=100,
                lastTrial=dict(time=event, price=price, size=20, bid=99, ask=101, serial=seconds),
                isTrial=True, bids=[dict(price=99, size=4)], asks=[dict(price=101, size=3)])


def test_archive_keeps_first_receipt_and_excludes_late_or_rest_inputs(tmp_path):
    day = date(2026, 11, 23)
    with Archive(tmp_path, day) as store:
        store.aggregate(record(day, '2330', 101, 40), session_us(day, time(8, 58, 41)))
        store.aggregate(record(day, '2330', 101, 40), session_us(day, time(8, 58, 50)))
        store.aggregate(record(day, '2330', 99, 59), session_us(day, time(8, 59)))
        store.trial('2330', record(day, '2330', 98, 58)['lastTrial'], session_us(day, time(13, 40)), 'rest')
        store.quote(dict(symbol='2330', date=day.isoformat(), previousClose=100, openPrice=103,
                         openTime=session_us(day, time(9, 2))), session_us(day, time(13, 40)), 'rest')
        store.conn.commit()
        result = store.panel(('2330', '2454'))
    assert result[0]['trial_gap'] == 101 / 100 - 1
    assert result[0]['actual_up'] is True
    assert result[0]['trial_receipts'] == {'websocket': 2, 'rest': 1}
    assert result[1]['actual_up'] is None


def test_locked_week_trains_only_on_earlier_fixed_ten_and_scores_missing(tmp_path):
    external = ('1216', '2002', '2412')
    universe = (*TRAINING_TICKERS, *external)
    for i, day in enumerate(COLLECTION_DAYS):
        with Archive(tmp_path, day) as store:
            for j, symbol in enumerate(universe):
                if i == 4 and symbol == '2412':
                    continue
                Up = (i + j) % 2 == 0
                trial = record(day, symbol, 102 if Up else 98, 50)
                store.aggregate(trial, session_us(day, time(8, 58, 51)))
                store.quote(dict(symbol=symbol, date=day.isoformat(), previousClose=100, openPrice=trial['lastTrial']['price'],
                                 openTime=session_us(day, time(9))), session_us(day, time(13, 40)), 'rest')
    report = evaluate_week(tmp_path, universe)
    assert report['train_rows'] == 40
    assert report['fit_through'] == '2026-11-26'
    assert report['validation']['expected'] == 13
    assert report['validation']['correct'] == 12
    assert report['validation']['unknown_answers'] == 1
    assert report['real_world_target_verified'] is False


def test_units_bound_every_phase_to_the_authorized_week():
    units = unit_files()
    assert set(units) == {f'twquant-opening-{phase}.{kind}'
                          for phase in ('collect', 'recover', 'evaluate') for kind in ('timer', 'service')}
    assert '2026-11-23..27 08:28..59:00 Asia/Taipei' in units['twquant-opening-collect.timer']
    assert '2026-11-27 14:10:00 Asia/Taipei' in units['twquant-opening-evaluate.timer']
    assert 'User=twquant-pilot' in units['twquant-opening-collect.service']


def test_week_preserves_factor_values_and_actual_receipt_for_validation(tmp_path):
    from math import isclose
    symbols = (*TRAINING_TICKERS, '1216')
    for i, day in enumerate(COLLECTION_DAYS):
        with Archive(tmp_path, day) as store:
            for j, ticker in enumerate(symbols):
                price = 102 if (i + j) % 2 else 98
                store.aggregate(record(day, ticker, price, 40), session_us(day, time(8, 58, 41)))
                store.quote(dict(date=day.isoformat(), symbol=ticker, previousClose=100, openPrice=price,
                                 openTime=session_us(day, time(9))), session_us(day, time(13, 40)), 'rest')
    result = evaluate_week(tmp_path, symbols)
    row = next(item for item in result['validation_rows'] if item['ticker'] == '1216')
    assert row['trial_received_us'] == session_us(COLLECTION_DAYS[-1], time(8, 58, 41))
    assert row['trial_exchange_us'] == session_us(COLLECTION_DAYS[-1], time(8, 58, 40))
    assert isclose(row['factor_base'] + sum(item['impact'] for item in row['factors']), row['probability_up'])
    assert next(item for item in row['factors'] if item['key'] == 'trial_gap')['value'] == 98 / 100 - 1


def test_auction_event_export_keeps_original_clocks_and_omits_recovery(tmp_path):
    import csv
    from twquant.opening_capture import export_auction_events

    day = COLLECTION_DAYS[0]
    with Archive(tmp_path, day) as store:
        timely = record(day, '2330', 101, 40)
        store.aggregate(timely, session_us(day, time(8, 58, 41)))
        store.trial('2330', record(day, '2330', 99, 52)['lastTrial'],
                    session_us(day, time(13, 40)), 'rest')
    output = tmp_path / 'events.csv'
    export_auction_events(tmp_path, ('2330',), output)
    with output.open() as stream:
        rows = {row['feature']: row for row in csv.DictReader(stream)}
    assert rows['auction_indicative_price']['value'] == '101.0'
    assert rows['auction_indicative_price']['published_at'] == '2026-11-23T08:58:40+08:00'
    assert rows['auction_indicative_price']['available_at'] == '2026-11-23T08:58:41+08:00'
    assert rows['auction_imbalance']['evidence_kind'] == 'historical'
