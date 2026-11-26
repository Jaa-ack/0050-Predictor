from datetime import date, time

from scripts import audit_opening_receipts as audit
from twquant.opening_capture import session_us


def test_official_answer_and_missing_receipt_days_stay_on_declared_grid():
    prior, first, second = [date(2026, 11, d) for d in (23, 24, 25)]
    exchange = session_us(first, time(8, 58, 58))
    rows = {str(first): [dict(ticker='2330', trial_exchange_us=exchange, trial_received_us=exchange + 1,
                               trial_gap=.01, trial_price=101, actual_up=False),
                         dict(ticker='2454', trial_exchange_us=exchange,
                              trial_received_us=session_us(first, time(8, 59)),
                              trial_gap=.01, trial_price=101, actual_up=True)]}
    bars = {prior: {'2330': (99., 100.), '2454': (99., 100.)},
            first: {'2330': (101., 99.), '2454': (100., 99.)},
            second: {'2330': (99., 101.)}}
    result = audit.score((prior, first, second), ['2330', '2454'], rows, bars, {})
    direct = result['models']['direct_trial']
    assert (direct['expected'], direct['answered'], direct['correct'], direct['known']) == (4, 2, 2, 3)
    assert direct['recall'] == {'up': 1., 'not_up': .5}
    assert result['timed_trial_inputs'] == 1
    assert result['missing_capture_dates'] == [str(second)]
    assert result['provider_direction_mismatches'] == 2
    assert result['models']['original']['paired_to_trial'] == dict(rescued=0, added_errors=2)
