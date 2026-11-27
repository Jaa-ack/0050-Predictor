import pandas as pd
import pytest


def test_override_requires_matching_raw_close_valid_base_and_eligible_clock():
    from scripts import research_opening_company_actions as study
    cutoff = pd.Timestamp('2026-11-27 08:59', tz='Asia/Taipei')
    rows = pd.DataFrame({
        'probability_up':[.8, .8, .8, None], 'reference_close':[100., 80., 100., 100.],
        'exchange_prior':[100.]*4, 'auction_basis':[98.]*4, 'ceiling':[107.]*4,
        'received_at':[cutoff-pd.Timedelta(seconds=1), cutoff, cutoff, cutoff],
        'decision_ts':[cutoff]*4})
    replay = study.directions(rows, strict=False)
    receipts = study.directions(rows, strict=True)
    assert replay['decline_2pct'].tolist()[:3] == [0., 1., 0.]
    assert receipts['decline_2pct'].tolist()[:3] == [0., 1., 1.]
    assert pd.isna(replay['decline_2pct'].iloc[-1])
    assert replay['ceiling_at_prior'].iloc[0] == 1.


def test_result_table_uses_auction_basis_and_rejects_dates_outside_verified_range():
    from scripts import research_opening_company_actions as study
    payload = dict(stat='OK', strDate='20261101', endDate='20261130',
                   fields=['資料日期','股票代號','除權息前收盤價','除權息參考價','開盤競價基準','漲停價格'],
                   data=[['115年11月27日','2330','100.00','97.12','98.00','107.50']])
    rows = study.parse(payload)
    assert rows.iloc[0].auction_basis == 98 and rows.iloc[0].date == pd.Timestamp('2026-11-27')
    payload['data'][0][0] = '115年12月01日'
    with pytest.raises(ValueError, match='range'):
        study.parse(payload)


def test_only_fixed_ten_choose_the_threshold_and_missing_forecasts_stay_in_grid():
    from scripts import research_opening_company_actions as study
    from twquant.universe import TRAINING_TICKERS
    days = pd.bdate_range('2026-01-01', periods=4)
    core = list(TRAINING_TICKERS)
    rows = pd.MultiIndex.from_product([days, core+['TEST']], names=['date','ticker']).to_frame(index=False)
    rows['decision_ts'] = rows.date.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=8, minutes=59)
    rows['received_at'] = pd.Timestamp('2026-11-27', tz='UTC')
    rows['label_available_at'] = rows.decision_ts + pd.Timedelta(hours=5)
    rows['probability_up'] = .9
    rows['reference_close'] = rows['exchange_prior'] = 100.
    rows['auction_basis'] = 98.
    rows['ceiling'] = 108.
    rows['actual_return'] = -.01
    rows.loc[rows.index[-1], 'probability_up'] = None
    def run():
        return study.compare(rows, days[:2], days[2:], core+['TEST'])
    initial = run()
    rows.loc[rows.ticker.eq('TEST') | rows.date.ge(days[2]), 'actual_return'] = .01
    result = run()
    assert result['selected'] == initial['selected'] == 'decline_1pct'
    assert result['calibration'] == initial['calibration']
    score = result['holdout']['constituents'][result['selected']]['score']
    assert score['expected'] == 22 and score['answered'] == 21
    assert result['recorded_receipts']['constituents'][result['selected']]['paired'] == {'rescued':0, 'added_errors':0}


def test_source_fetch_keeps_separate_receipts_and_checks_echoed_date_range(tmp_path, monkeypatch):
    import json
    from datetime import date
    from types import SimpleNamespace
    from scripts import research_opening_company_actions as study
    day = date(2026, 11, 27)
    payload = dict(stat='OK', strDate='20261127', endDate='20261127')
    def request(url, *, params, timeout):
        assert params['startDate'] == params['endDate'] == '20261127'
        return SimpleNamespace(content=json.dumps(payload).encode(), raise_for_status=lambda: None)
    monkeypatch.setattr('requests.get', request)
    first = study.fetch(tmp_path, day, day)
    second = study.fetch(tmp_path, day, day)
    assert first != second and first.read_bytes() == second.read_bytes()
    assert json.loads(first.with_suffix('.meta.json').read_text())['received_at']
    payload['endDate'] = '20261126'
    with pytest.raises(ValueError, match='range'):
        study.fetch(tmp_path, day, day)
