import pandas as pd
import pytest


def test_regional_inputs_skip_japan_holidays_and_use_only_prior_taiwan_fx():
    from scripts import research_opening_regional as study
    days = pd.to_datetime(['2026-11-20', '2026-11-23', '2026-11-24', '2026-11-25'])
    index = pd.DataFrame({'Open':[100., 110., 999.], 'Close':[100., 200., 1234.]}, index=days[[0, 2, 3]])
    fx = pd.Series([30., 33., 66.], index=days[[0, 1, 3]])
    features = study.regional_returns(days, index, fx)
    assert pd.isna(features.loc[days[1], 'japan'])
    assert features.loc[days[2], 'japan'] == pytest.approx(.10)
    assert features.loc[days[2], 'fx'] == pytest.approx(.10)
    assert pd.isna(features.loc[days[3], 'fx'])


def test_holdout_keeps_missing_forecasts_and_external_labels_cannot_select_models():
    from scripts import research_opening_regional as study
    from twquant.universe import TRAINING_TICKERS
    dates = pd.bdate_range('2026-01-01', periods=14)
    symbols = [*TRAINING_TICKERS, 'TEST']
    frame = pd.MultiIndex.from_product([dates, symbols], names=['date', 'ticker']).to_frame(index=False)
    frame['actual_return'] = [.01 if i % 3 == 0 else -.01 for i in range(len(frame))]
    frame['control_context'] = [.55 if i % 2 == 0 else .45 for i in range(len(frame))]
    frame['decision_ts'] = frame.date.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=8, minutes=59)
    frame['label_available_at'] = frame.decision_ts + pd.Timedelta(hours=5)
    frame = frame.drop(frame.index[-1])
    features = pd.DataFrame({'japan':[.01, -.01]*7, 'fx':[.001, -.002]*7}, index=dates)
    def run():
        return study.compare(frame, features, dates[-3:], symbols, calibration_days=4)
    first = run()
    frame.loc[frame.ticker.eq('TEST') | frame.date.ge(dates[-3]), 'actual_return'] *= -1
    second = run()
    assert first['selected'] == second['selected'] and first['calibration'] == second['calibration']
    for result in first['holdout']['constituents'].values():
        assert result['score']['expected'] == 3 * len(symbols)
        assert result['score']['answered'] == 3 * len(symbols) - 1


def test_repeat_download_preserves_first_receipt_and_raw_bytes(tmp_path, monkeypatch):
    from scripts import research_opening_regional as study
    from types import SimpleNamespace
    payloads = iter([b'first', b'next'])
    monkeypatch.setattr('requests.get', lambda *args, **kwargs: SimpleNamespace(
        content=next(payloads), raise_for_status=lambda: None))
    first = study.download(tmp_path, 'japan')
    second = study.download(tmp_path, 'japan')
    assert first != second
    assert study.snapshot(first)[0] == b'first'
    assert study.snapshot(second)[0] == b'next'
