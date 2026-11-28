import pandas as pd


def test_event_gate_excludes_stale_or_late_receipts_and_ambiguous_headlines():
    from scripts import research_opening_issuer_events as study
    aliases = {'2330':['台積電','TSMC'], '2454':['聯發科','MediaTek']}
    assert study.title_issuer('TSMCX outlook',aliases) is None
    assert study.title_issuer('TSMC and MediaTek outlook',aliases) is None
    assert study.title_issuer('台積電公布營運結果',aliases) == '2330'
    start = pd.Timestamp('2026-11-27 13:30',tz='Asia/Taipei')
    due = pd.Timestamp('2026-11-30 08:59',tz='Asia/Taipei')
    middle = start + pd.Timedelta(hours=1)
    data = [('2330','disclosure',start,middle),('2330','financial',middle,due),
            ('2454','headline',middle,middle),('2454','conference',due,due)]
    assert study.available(data,start,due,strict=True) == {'headline':{'2454'}}
    assert study.available(data,start,due,strict=False) == {'headline':{'2454'},'financial':{'2330'}}


def test_same_count_control_matches_each_day_and_availability():
    from scripts import research_opening_issuer_events as study
    frame = pd.DataFrame(dict(date=['d1','d1','d1','d2'], ticker=['2330','2454','2317','2330'],
                              base=[0.,0.,0.,0.], direct_trial=[True,True,False,True],
                              trial_fallback=[False,False,True,False], actual_return=[1.,-1.,-1.,1.]))
    switch = pd.Series([True,False,True,False])
    control = study.same_count(frame, 'base', switch)
    assert control['eligible_switched'] == 1
    assert control['observed_net'] == 1
    assert control['expected_rescued'] == control['expected_added_errors'] == .5
