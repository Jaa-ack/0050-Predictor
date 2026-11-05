import json

import pandas as pd
import pytest

from twquant.credentials import Credentials
from twquant.source_workflows import source_status,sync_sources,reconcile_prices


def test_status_has_all_sources_but_no_secret_values():
    c=Credentials({'FUGLE_API_KEY':'example-private-value'})
    statuses=source_status(c)
    assert {s['provider'] for s in statuses}>={'finmind','fred','fmp','newsapi','twinkle','fugle','fubon','twse','taifex'}
    assert next(s for s in statuses if s['provider']=='fugle')['status']=='configured_unverified'
    assert next(s for s in statuses if s['provider']=='fred')['status']=='missing_credentials'
    assert 'example-private-value' not in json.dumps(statuses)


def test_sync_skips_missing_keys_and_archives_available_public_data(tmp_path,monkeypatch):
    from twquant.sources import SourceResult
    plan=tmp_path/'plan.json'
    plan.write_text(json.dumps({'jobs':[
        {'id':'cpi','enabled':True,'provider':'fred','dataset':'CPIAUCSL','start':'2020-01-01','end':'2025-01-01'},
        {'id':'exchange','enabled':True,'provider':'twse','dataset':'prices'}]}))
    monkeypatch.setattr('twquant.source_workflows.fetch_public',lambda *a:SourceResult('twse','prices','official_snapshot',
                        [{'Code':'2330','ClosingPrice':'100'}],'2026-11-04T00:00:00Z'))
    result=sync_sources(tmp_path,plan,Credentials({}))
    assert result['jobs'][0]['status']=='missing_credentials'
    assert result['jobs'][1]['status']=='ok'
    assert result['jobs'][1]['records']==1
    assert (tmp_path/'sources'/'sync_report.json').is_file()


def test_price_reconciliation_never_silently_merges_conflicting_vendors():
    left=pd.DataFrame({'ticker':['2330'],'date':pd.to_datetime(['2025-01-02']),'close':[100.]})
    right=left.assign(close=120.)
    r=reconcile_prices(left,right)
    assert not r['passed']
    assert r['mismatches']==1
