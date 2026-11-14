from types import SimpleNamespace
from pathlib import Path
import importlib.util
import json

import pytest


def module():
    assert importlib.util.find_spec('twquant.live_market'), 'Live market adapter is missing'
    from twquant import live_market
    return live_market


class Stock:
    def __init__(self):self.handlers={};self.requests=[]
    def on(self,event,callback):self.handlers[event]=callback
    def connect(self):self.connected=True
    def subscribe(self,value):self.requests.append(value)
    def disconnect(self):self.connected=False


def test_fubon_adapter_subscribes_to_read_only_channels_and_sanitizes_errors():
    m=module();stock=Stock();sdk=SimpleNamespace(marketdata=SimpleNamespace(websocket_client=SimpleNamespace(stock=stock)))
    messages=[];gaps=[];feed=m.FubonFeed(['2330','0050','IX0001'],sdk=sdk)
    feed.connect(messages.append,gaps.append)
    assert stock.requests==[{'channel':'trades','symbols':['2330','0050']},
        {'channel':'books','symbols':['2330','0050']},{'channel':'indices','symbol':'IX0001'}]
    stock.handlers['message'](json.dumps({'event':'error','data':{'message':'secret-account-token'}}))
    assert gaps==['provider_error'] and not messages
    stock.handlers['message'](json.dumps({'event':'data','channel':'trades','data':{'symbol':'2330','price':100}}))
    assert messages[0]['data']['price']==100
    feed.close();assert not stock.connected


def test_subscription_acknowledgements_are_distinct_and_do_not_imply_market_data():
    m=module();stock=Stock();sdk=SimpleNamespace(marketdata=SimpleNamespace(websocket_client=SimpleNamespace(stock=stock)))
    feed=m.FubonFeed(['2330','IX0001'],sdk=sdk);feed.connect(lambda _:None,lambda _:None)
    ack=dict(event='subscribed',data=[dict(channel='trades',symbol='2330'),dict(channel='books',symbol='2330')])
    stock.handlers['message'](json.dumps(ack));stock.handlers['message'](json.dumps(ack))
    assert len(feed.subscriptions)==2 and not feed.ready
    stock.handlers['message'](json.dumps(dict(event='subscribed',data=dict(channel='indices',symbol='IX0001'))))
    assert feed.ready


def test_launch_agents_use_argument_arrays_and_poll_by_taipei_clock(tmp_path):
    m=module();python=tmp_path/'environment/bin/python';python.parent.mkdir(parents=True);python.touch()
    root=tmp_path/'artifacts/pilot';root.mkdir(parents=True)
    value=m.launch_agent(tmp_path,python,root,'collect')
    assert value['RunAtLoad'] and value['StartInterval']==60
    assert str(python) in value['ProgramArguments']
    assert value['WorkingDirectory']==str(tmp_path.resolve())
    assert all('shell' not in a for a in value['ProgramArguments'])
    assert value['EnvironmentVariables']['TZ']=='Asia/Taipei'
    with pytest.raises(ValueError):m.launch_agent(tmp_path,Path('/untrusted/python'),root,'collect')


def test_independent_forecast_job_starts_before_decision_even_when_feed_is_down(tmp_path):
    m=module();python=tmp_path/'environment/bin/python';python.parent.mkdir(parents=True);python.touch()
    root=tmp_path/'artifacts/pilot';root.mkdir(parents=True)
    (root/'protocol.json').write_text(json.dumps({'dates':['2026-11-16','2026-11-17'],'prediction_time':'10:00:00'}))
    job=m.launch_agent(tmp_path,python,root,'forecast')
    assert 'forecast' in job['ProgramArguments']
    assert len(job['StartCalendarInterval'])==2
    assert all(item['Minute']==59 for item in job['StartCalendarInterval'])


def test_retirement_removes_only_this_pilots_jobs(tmp_path,monkeypatch):
    import plistlib
    m=module();home=tmp_path/'home';directory=home/'Library/LaunchAgents';directory.mkdir(parents=True)
    root=tmp_path/'pilot';root.mkdir()
    own=directory/(m.LABEL+'.collect.plist');foreign=directory/(m.LABEL+'.report.plist')
    own.write_bytes(plistlib.dumps(dict(Label=m.LABEL+'.collect',ProgramArguments=['python','--root',str(root)])))
    foreign.write_bytes(plistlib.dumps(dict(Label=m.LABEL+'.report',ProgramArguments=['python','--root','/other/pilot'])))
    monkeypatch.setattr(m.Path,'home',classmethod(lambda cls:home))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    m.retire_jobs(root)
    assert not own.exists() and foreign.exists()


def test_scheduled_forecast_emits_full_fallback_grid_without_connecting_a_provider(tmp_path,monkeypatch):
    from datetime import timedelta
    from test_live_pilot import protocol,stamp
    from twquant.live_pilot import PilotStore
    m=module();store=PilotStore(tmp_path,dict(protocol(),require_release_seal=True))
    clock=[stamp(-.02)]
    monkeypatch.setattr(m,'now',lambda:clock[0])
    monkeypatch.setattr(m.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    result=m.forecast(tmp_path,store)
    assert result['state']=='forecast_recorded'
    rows=store.emit(stamp(.5))
    assert len(rows)>=50 and all(r['fallback'] for r in rows)
    assert store.db.execute('SELECT count(*) FROM releases').fetchone()[0]==1
