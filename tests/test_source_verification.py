import json

import pandas as pd
import pytest

from twquant.credentials import Credentials
from twquant.sources import SourceResult,SourceHTTPError


def test_live_verification_distinguishes_partial_entitlement_and_redacts_errors(tmp_path,monkeypatch):
    from twquant import source_verification as module
    secret='example-verification-private-key'
    def fetch(t,dataset,symbol,start,end,**kwargs):
        if symbol=='QQQ':raise SourceHTTPError('fmp',402)
        if dataset=='estimates':raise RuntimeError('reflected '+secret)
        return SourceResult('fmp',dataset,'market' if dataset=='adjusted_prices' else 'financial',
            [{'symbol':symbol,'date':'2025-01-02','adjClose':100,'eps':1}],
            '2026-11-05T00:00:00Z',{'symbol':symbol,'complete':dataset=='adjusted_prices'})
    monkeypatch.setattr(module,'fetch_fmp',fetch)
    result=module.verify_sources(tmp_path,Credentials({'FMP_API_KEY':secret}),providers=['fmp'])
    assert not result['passed']
    assert result['providers'][0]['status']=='partial'
    assert next(x for x in result['checks'] if x.get('http_status')==402)['status']=='entitlement_blocked'
    assert secret not in json.dumps(result)
    assert secret not in (tmp_path/'sources/verification.json').read_text()
    assert list((tmp_path/'sources/verifications').glob('*/report.json'))


def test_fubon_verification_stops_before_login_for_missing_prerequisites(tmp_path):
    from twquant.source_verification import verify_sources
    result=verify_sources(tmp_path,Credentials({}),providers=['fubon'])
    row=result['checks'][0]
    assert row['status']=='blocked'
    assert 'missing_credentials' in row['blockers']
    assert row['login_attempted'] is False


def test_price_probe_validates_snapshot_without_replacing_primary_history(tmp_path,monkeypatch):
    from twquant import source_verification as module
    from twquant.demo import make_demo_prices
    marker=tmp_path/'data/current.json'
    marker.parent.mkdir()
    marker.write_text('existing primary snapshot')
    prices=make_demo_prices(30).assign(source='fugle')
    monkeypatch.setattr(module,'fetch_fugle_prices',lambda *a:prices)
    monkeypatch.setattr(module,'fetch_fugle',lambda *a:SourceResult('fugle','holdings','membership',[],
        '2026-11-05T00:00:00Z',{'symbol':'0050'}))
    result=module.verify_sources(tmp_path,Credentials({'FUGLE_API_KEY':'example-fugle-key'}),providers=['fugle'])
    price=next(x for x in result['checks'] if x['check']=='paired-prices')
    assert price['status']=='passed'
    assert price['model_prices'] is True
    assert marker.read_text()=='existing primary snapshot'
    assert any(x['status']=='empty' for x in result['checks'])


def test_empty_public_news_batch_is_distinguished_from_api_failure(tmp_path,monkeypatch):
    from twquant import source_verification as module
    monkeypatch.setattr(module,'fetch_gdelt',lambda *a:SourceResult('gdelt','gkg','news',[],
        '2026-11-05T00:00:00Z',{'complete':True}))
    result=module.verify_sources(tmp_path,Credentials({}),providers=['gdelt'])
    assert result['providers'][0]['status']=='empty'
    assert result['checks'][0]['schema_validated'] is True


@pytest.mark.parametrize('reject_login,stage,status,code',[
    (True,'login','authentication_rejected','record_not_found'),
    (False,'market_data','failed','market_data_initialization_failed')])
def test_fubon_login_rejection_is_distinct_from_market_data_failure(tmp_path,monkeypatch,reject_login,stage,status,code):
    import sys
    import types
    from importlib.machinery import ModuleSpec
    from types import SimpleNamespace
    from twquant import source_verification as module
    certificate=tmp_path/'cert.p12'
    certificate.write_bytes(b'fixture-only');certificate.chmod(0o600)
    class SDK:
        def apikey_login(self,*args):
            return SimpleNamespace(is_success=not reject_login,message='查無資料',data='private-account-result')
        def init_realtime(self):
            if reject_login:raise AssertionError('Must not initialize after rejected login')
            raise RuntimeError('example-private-key private-account-result')
    sdk_module=types.ModuleType('fubon_neo.sdk');sdk_module.FubonSDK=SDK
    package=types.ModuleType('fubon_neo');package.__spec__=ModuleSpec('fubon_neo',loader=None)
    monkeypatch.setitem(sys.modules,'fubon_neo',package)
    monkeypatch.setitem(sys.modules,'fubon_neo.sdk',sdk_module)
    monkeypatch.setattr(module,'_fubon_prerequisites',lambda *a:{'blockers':[]})
    credentials=Credentials({'FUBON_API_KEY':'example-private-key','FUBON_PERSONAL_ID':'example-private-id',
        'FUBON_CERT_PATH':str(certificate),'FUBON_CERT_PASSWORD':'example-password'})
    result=module.verify_sources(tmp_path,credentials,providers=['fubon'])
    row=result['checks'][0]
    assert row['status']==status
    assert row['auth_stage']==stage
    assert row['failure_code']==code
    assert row['login_attempted'] is True
    assert 'private-account-result' not in json.dumps(result)
    assert 'example-private-key' not in json.dumps(result)
