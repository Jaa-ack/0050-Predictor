import importlib.util
import json

import numpy as np
import pandas as pd
import pytest


def module():
    assert importlib.util.find_spec('twquant.queue_ceiling') is not None, 'Privileged-state diagnostic is missing'
    from twquant import queue_ceiling
    return queue_ceiling


def test_independent_forks_preserve_past_states_and_chunking_without_emitting_forecasts():
    from twquant.intraday_queue import QueueScenario
    dates=pd.bdate_range('2020-01-02',periods=4)
    kwargs=dict(seed=17,branch_seed=73,branches=16,horizons_seconds=[1,5,30,60])
    scenario=QueueScenario('fixture',liquidity_response=1.)
    a=module().queue_ceiling_panel(['2330','1216'],dates,scenario,chunk_dates=1,**kwargs)
    b=module().queue_ceiling_panel(['2330','1216'],dates,scenario,chunk_dates=4,**kwargs)
    pd.testing.assert_frame_equal(a,b)
    changed=module().queue_ceiling_panel(['2330','1216'],dates,scenario,chunk_dates=4,**dict(kwargs,branch_seed=97))
    pd.testing.assert_frame_equal(a[['ticker','date','horizon_seconds','state_sha256']],changed[['ticker','date','horizon_seconds','state_sha256']])
    assert not a.eval_up.equals(changed.eval_up)
    smaller=module().queue_ceiling_panel(['2330','1216'],dates,scenario,chunk_dates=2,**dict(kwargs,branches=8))
    for label in ('up','down','flat'):
        np.testing.assert_array_equal(a[f'pilot_{label}'],smaller[f'pilot_{label}']+smaller[f'eval_{label}'])
    expanded=module().queue_ceiling_panel(['2330','1216','0050'],pd.bdate_range('2020-01-02',periods=5),scenario,chunk_dates=2,**kwargs)
    keep=expanded[expanded.ticker.isin(['2330','1216'])&expanded.date.isin(dates)].reset_index(drop=True)
    pd.testing.assert_frame_equal(a,keep)
    assert len(a)==2*4*4
    assert a[['pilot_up','pilot_down','pilot_flat']].sum(axis=1).eq(8).all()
    assert a[['eval_up','eval_down','eval_flat']].sum(axis=1).eq(8).all()
    assert not {'probability_up','actual_return','model_version'}&set(a)
    assert not any(c.startswith('f_book_') for c in a)


def counts_fixture(flat=False):
    dates=pd.bdate_range('2020-01-02',periods=4)
    rows=[]
    for ticker in ['2330','1216']:
        for date in dates:
            rows.append(dict(ticker=ticker,date=date,horizon_seconds=30,branches=8,
                pilot_up=0 if flat else 3,pilot_down=0 if flat else 1,pilot_flat=4 if flat else 0,
                eval_up=0 if flat else 1,eval_down=0 if flat else 2,eval_flat=4 if flat else 1))
    return pd.DataFrame(rows),dates


def test_ceiling_summary_uses_jensen_upper_and_independent_pilot_policy_lower():
    frame,dates=counts_fixture()
    r=module().summarize_queue_ceiling(frame,['2330','1216'],dates,family_size=2,alpha=.05)
    item=r['horizons']['30']['per_stock']['2330']
    assert item['strict']['plugin_upper_point']==.5
    assert item['strict']['split_policy_point']==.25
    # Pilot chooses up even though independent evaluation has more down/nonup.
    assert item['up_nonup']['plugin_upper_point']==.5
    assert item['up_nonup']['split_policy_point']==.25
    expected=np.sqrt(np.log(4*2/.05)/(2*4))
    assert r['horizons']['30']['hoeffding_radius']==pytest.approx(expected)
    assert item['strict']['upper']==min(1.,.5+expected)
    assert item['strict']['lower']==max(0.,.25-expected)
    assert not r['forecast_produced'] and not r['target_feasibility_established']
    assert r['privileged_state_used'] and r['models_trained']==0
    with pytest.raises(ValueError,match='complete'):
        module().summarize_queue_ceiling(frame.iloc[1:],['2330','1216'],dates,family_size=2)
    malformed=frame.copy();malformed.loc[0,'eval_flat']=9
    with pytest.raises(ValueError,match='counts'):
        module().summarize_queue_ceiling(malformed,['2330','1216'],dates,family_size=2)


def test_all_flat_paths_have_zero_strict_credit_and_full_nonup_credit():
    frame,dates=counts_fixture(flat=True)
    r=module().summarize_queue_ceiling(frame,['2330','1216'],dates,family_size=2)
    item=r['horizons']['30']['per_stock']['1216']
    assert item['strict']['plugin_upper_point']==item['strict']['split_policy_point']==0.
    assert item['up_nonup']['plugin_upper_point']==item['up_nonup']['split_policy_point']==1.


def test_ceiling_rejects_index_books_and_invalid_random_or_calendar_contracts():
    from twquant.intraday_queue import QueueScenario
    fn=module().queue_ceiling_panel;dates=pd.bdate_range('2020-01-02',periods=2)
    for kwargs in [dict(branches=3),dict(branches=5),dict(seed=True),dict(horizons_seconds=[2]),dict(chunk_dates=0)]:
        with pytest.raises(ValueError):fn(['2330'],dates,QueueScenario('fixture'),**kwargs)
    with pytest.raises(ValueError,match='index'):
        fn(['IX0001'],dates,QueueScenario('fixture'))
    with pytest.raises(ValueError):fn(['2330'],dates.append(dates),QueueScenario('fixture'))


def test_ceiling_cli_freezes_before_forking_without_credentials_or_model_selection(tmp_path,capsys,monkeypatch):
    from twquant.universe import TRAINING_TICKERS
    import twquant.cli as cli
    def forbidden():raise AssertionError('Offline ceiling diagnostic must not load credentials')
    monkeypatch.setattr(cli,'load_dotenv',forbidden)
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'ceiling.json'
    config.write_text(json.dumps(dict(state_dates=126,calendar_offset=800,branches=4,seeds=[17],branch_seed=73,
        horizons_seconds=[1],scenarios=[dict(name='fixture')],chunk_dates=32,alpha=.05)))
    output=tmp_path/'ceiling';m=module();original=m.queue_ceiling_panel
    def checked(*args,**kwargs):
        protocol=json.loads((output/'protocol.json').read_text())
        assert protocol['privileged_state_used'] and protocol['models_trained']==0
        assert protocol['family_size']==50
        return original(*args,**kwargs)
    monkeypatch.setattr(m,'queue_ceiling_panel',checked)
    args=['direction-queue-ceiling','--config',str(config),'--membership',str(membership),'--out',str(output)]
    assert cli.main(args)==0
    response=json.loads(capsys.readouterr().out)
    assert response['models_trained']==0 and response['forecast_produced'] is False
    r=json.loads((output/'report.json').read_text())
    assert len(r['experiments'])==1 and not r['target_feasibility_established']
    assert r['experiments'][0]['summary']['horizons']['1']['states']==126*50
    assert cli.main(args)==2
    assert 'fresh' in capsys.readouterr().err
