from datetime import date, datetime, time
import json

from twquant import opening_capture as capture
from twquant import opening_compare
from twquant.universe import TRAINING_TICKERS


def test_forward_capture_uses_frozen_dates_members_and_existing_receipt_archive(tmp_path, monkeypatch):
    from scripts import opening_forward as forward
    day = date(2026, 11, 30)
    members = [*TRAINING_TICKERS, *[str(n) for n in range(4000, 4040)]]
    protocol = dict(sessions=[str(day)], tickers=members)
    called = []
    def collect(store, symbols):
        called.append((store.day, list(symbols)))
        store.status('test_ack')
        return True
    monkeypatch.setattr(capture, '_collect', collect)
    for clock in (datetime(2026, 11, 29, 8, 30, tzinfo=capture.TZ),
                  datetime.combine(day, time(9, 11), capture.TZ)):
        assert forward.acquire('collect', protocol, tmp_path, clock) == 0
    assert not list(tmp_path.iterdir())
    assert forward.acquire('collect', protocol, tmp_path, datetime.combine(day, time(8, 30), capture.TZ)) == 0
    assert called == [(day, members)]
    with capture.Archive(tmp_path, day, readonly=True) as store:
        assert store.done('test_ack', '')


def test_settlement_retries_unpublished_official_bars_keeps_the_full_denominator(tmp_path, monkeypatch):
    from scripts import opening_forward as forward
    from types import SimpleNamespace
    prior = date(2026, 11, 27)
    dates = ['2026-11-30', '2026-12-01']
    protocol = dict(task='0859_open_above_previous_session_close', sessions=dates, tickers=['2330','2454'])
    requested = []
    published = {'20261127'}
    def response(url, *, params, timeout):
        day = params['date']
        requested.append(day)
        payload = dict(stat='OK', date=day, tables=[dict(fields=['證券代號','開盤價','收盤價'],
                       data=[['2330','101','100'],['2454','99','100']])]) if day in published else dict(stat='pending')
        return SimpleNamespace(json=lambda:payload, raise_for_status=lambda:None, url=url)
    monkeypatch.setattr('requests.get', response)
    now = datetime(2026, 11, 30, 14, 20, tzinfo=capture.TZ)
    clock = lambda: int(now.timestamp()) * 10**9
    first = forward.settle(protocol, 'a'*64, prior, tmp_path, clock)
    assert first['unavailable_bar_dates'] == [dates[0]]
    assert first['score']['expected'] == 2 and first['score']['known'] == 0
    published.add('20261130')
    second = forward.settle(protocol, 'a'*64, prior, tmp_path, clock)
    assert requested == ['20261127','20261130','20261130']
    assert second['unavailable_bar_dates'] == [] and second['score']['known'] == 2
    assert second['missing_sessions'] == dates[:1] and second['pending_sessions'] == dates[1:]
    assert second['score']['correct'] == 0 and second['score']['answered'] == 0
    assert json.loads((tmp_path/'delivery-audit.json').read_text()) == second


def test_future_recovery_preserves_ascending_retry_with_frozen_symbols(tmp_path, monkeypatch):
    from scripts import opening_forward as forward
    day = date(2026, 11, 30)
    tickers = [*TRAINING_TICKERS, *[str(n) for n in range(4000, 4040)]]
    attempts = []
    def failed_request(store, symbols):
        raise TimeoutError()
    def supplement(root, days, symbols):
        attempts.append((root, days, symbols))
        return 0
    monkeypatch.setattr(capture, '_recover', failed_request)
    monkeypatch.setattr(opening_compare, '_supplement', supplement)
    result = forward.acquire('recover', dict(sessions=[str(day)], tickers=tickers), tmp_path,
                             datetime.combine(day, time(13, 40), capture.TZ))
    assert result == 0 and attempts == [(tmp_path, (day,), tickers)]


def test_wrong_pin_prevents_collection_before_any_root_is_created(tmp_path, monkeypatch):
    from scripts import opening_forward as forward
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle/'protocol.json').write_text('{}')
    monkeypatch.setattr(forward.rule, 'load', lambda _: dict(tickers=list(TRAINING_TICKERS)))
    root = tmp_path/'unopened'
    assert forward.main(['collect','--bundle',str(bundle),'--root',str(root),'--protocol-sha256','0'*64]) == 1
    assert not root.exists()
