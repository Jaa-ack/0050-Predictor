import pandas as pd
import pytest


def test_issuer_return_uses_only_completed_us_sessions_and_obeys_receipt_clock():
    from scripts import research_opening_adr as study
    days = pd.to_datetime(['2026-11-20', '2026-11-24', '2026-11-25', '2026-11-26', '2026-11-27'])
    us = pd.to_datetime(['2026-11-20', '2026-11-23', '2026-11-24', '2026-11-25'])
    stamp = pd.Timestamp('2026-11-26 08:59', tz='Asia/Taipei')
    def series(returns):
        return pd.Series(returns, index=us), stamp
    bars = {'SPY':series([0., .01, 0., 0.]), 'TSM':series([.10, -.10, .2, .03])}
    result = study.signals(days, bars, strict=False)
    assert result.loc[(days[1],'2330'),'own'] == pytest.approx(-.01)
    assert result.loc[(days[1],'2330'),'relative'] == pytest.approx(.99/1.01-1)
    assert pd.isna(result.loc[(days[-1],'2330'),'own'])
    assert pd.isna(result.loc[(days[1],'3711'),'own'])
    assert study.signals(days, bars, strict=True).own.isna().all()


def test_adr_cannot_change_unmapped_missing_or_confident_base_predictions():
    from scripts import research_opening_adr as study
    part = pd.DataFrame(dict(probability_up=[.45,.1,float('nan'),.45], own=[.01,.02,.03,None],
                             relative=[-.01,-.02,-.03,None]))
    v = study.directions(part)
    assert v.own_10.tolist()[:2] == [1.,0.]
    assert pd.isna(v.own_10.iloc[2])
    assert v.own_10.iloc[3] == 0.
    assert v.relative_10.iloc[0] == 0.


def test_only_fixed_ten_select_adr_rule_and_missing_holdout_rows_are_scored():
    from scripts import research_opening_adr as study
    from twquant.universe import TRAINING_TICKERS
    days = pd.bdate_range('2026-03-02', periods=5)
    symbols = [*TRAINING_TICKERS, '2412']
    grid = pd.MultiIndex.from_product([days[1:],symbols],names=['date','ticker'])
    frame = grid.to_frame(index=False)
    frame['probability_up'] = .49
    frame['actual_return'] = -.01
    frame.loc[frame.ticker.eq('2330'),'actual_return'] = .01
    frame['decision_ts'] = frame.date.dt.tz_localize('Asia/Taipei') + pd.Timedelta(hours=8, minutes=59)
    frame['label_available_at'] = frame.decision_ts + pd.Timedelta(hours=5)
    frame = frame.iloc[:-1].copy()
    def sample():
        return pd.Series(.02,index=days),pd.Timestamp('2026-11-28',tz='UTC')
    bars = {s:sample() for s in ('SPY','TSM','CHT')}
    def run():
        return study.compare(frame, days, days[-2:], symbols, bars, calibration_days=2)
    first = run()
    frame.loc[frame.ticker.eq('2412') | frame.date.ge(days[-2]),'actual_return'] *= -1
    second = run()
    assert first['selected'] == second['selected'] == 'own_10'
    assert first['calibration'] == second['calibration']
    r = first['holdout']['constituents']['own_10']['score']
    assert r['expected'] == 22 and r['answered'] == 21


def test_continuous_adr_weight_uses_only_mature_fixed_ten_observations():
    from scripts import research_opening_adr as study
    day = pd.Timestamp('2026-04-01', tz='Asia/Taipei')
    data = pd.DataFrame(dict(ticker=['2330','3711','2412','2330'], own=[.01,-.01,1.,1.],
                             probability_up=[.5]*4, actual_return=[.01,-.01,-1.,-1.],
                             label_available_at=[day-pd.Timedelta(days=1)]*3+[day]))
    weight, count = study.fit_weight(data, 'own', day)
    assert count == 2 and weight > 0
    assert study.fit_weight(data.iloc[:2], 'own', day) == (weight, count)


def test_partial_us_session_in_a_download_never_becomes_historical_eod(tmp_path):
    from scripts import research_opening_adr as study
    from twquant.sources import SourceResult
    from twquant.source_store import SourceStore
    rows = [dict(date=d, stock_id='SPY', Open=100., Close=101., Volume=1000)
            for d in ('2026-11-25','2026-11-27')]
    result = SourceResult('finmind','USStockPrice','market',rows,'2026-11-27T16:55:00Z',dict(symbol='SPY'))
    SourceStore(tmp_path).write(result)
    series, receipt = study.load_bars(tmp_path)[0]['SPY']
    assert list(series.index) == [pd.Timestamp('2026-11-25')]
    assert receipt == pd.Timestamp(result.retrieved_at)
