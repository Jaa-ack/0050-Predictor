import pandas as pd
import pytest

from scripts import research_opening_capture_models as study


def test_same_grid_retains_missing_trial_and_switches_to_backup_only_for_explicit_fallback():
    rows = pd.DataFrame({'ticker':['2330','2454','2317'], 'actual_return':[.02,0.,None],
                         'direct_trial':[1.,0.,float('nan')], 'trial_fallback':[False,True,None], 'peer_vote':[False,True,True]})
    results = study.compare(rows, {'context':[.2,.9,.6]}, 'context', list(rows.ticker))
    assert results['context']['score']['expected'] == 3
    assert results['context']['paired_to_trial'] == {'rescued':0,'added_errors':2}
    assert results['trial_with_backup']['score']['answered'] == 2
    assert results['trial_with_backup']['paired_to_trial'] == {'rescued':0,'added_errors':1}


def test_snapshot_timestamp_exactly_at_decision_is_excluded():
    source = pd.DataFrame({'ticker':['2330'],'f_intraday':[.02]})
    due = pd.Timestamp('2026-11-23 08:59', tz='Asia/Taipei')
    assert study.visible(source, due, due, 'actual_receipts').empty
    assert study.visible(source, due-pd.Timedelta(microseconds=1), due, 'actual_receipts').equals(source)
    assert study.visible(source, due+pd.Timedelta(days=2), due, 'historical_reconstruction').equals(source)
    with pytest.raises(ValueError):
        study.visible(source, pd.Timestamp('2026-11-20'), due, 'actual_receipts')


def test_core_vote_ignores_self_and_all_external_stocks_and_requires_observed_peers():
    from twquant.universe import TRAINING_TICKERS
    core = list(TRAINING_TICKERS)
    symbols = core + ['2002','1216']
    trial = {s:dict(direct=(i<5), direct_fallback=False) for i,s in enumerate(symbols)}
    trial['2002'] = dict(direct=False, direct_fallback=True)
    original = study.core_votes(trial,symbols)
    assert original[core[0]] is False and original[core[-1]] is True
    assert original['2002'] is False
    trial['1216']['direct'] = True
    assert study.core_votes(trial,symbols) == original
    trial[core[0]] = dict(direct=False,direct_fallback=True)
    updated = study.core_votes(trial,symbols)
    assert updated[core[0]] == original[core[0]]
    assert updated['2002'] is None and updated[core[1]] is None


def test_prior_price_and_two_session_flow_lag_use_separate_receipt_clocks():
    from datetime import date
    days = [date(2026,11,d) for d in (18,19,20,23)]
    ticker = '2330'
    before = '2026-11-23T08:58:59+08:00'
    after = '2026-11-23T09:00:01+08:00'
    prior = pd.DataFrame(dict(close=[100.],f_intraday=[.01]),index=[ticker])
    outcome = pd.DataFrame(dict(opening=[101.],f_intraday=[.99]),index=[ticker])
    flow = pd.DataFrame({study.FLOW[0]:[.2]},index=[ticker])
    bars = {days[-2]:(prior,before),days[-1]:(outcome,after)}
    flows = {days[0]:(flow,after),days[-2]:(flow*2,before)}
    one = study.panel(days,[ticker],bars,flows,[],{},'actual_receipts').iloc[0]
    assert one.f_intraday == .01 and pd.isna(one[study.FLOW[0]]) and one.actual_return == pytest.approx(.01)
    reconstructed = study.panel(days,[ticker],bars,flows,[],{},'historical_reconstruction').iloc[0]
    assert reconstructed[study.FLOW[0]] == .2 and pd.isna(reconstructed.direct_trial)
