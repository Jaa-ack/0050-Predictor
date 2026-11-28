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


def test_cna_job_keeps_publisher_clock_and_receipt_without_fetching_the_article():
    from types import SimpleNamespace
    from twquant.source_workflows import _execute_job
    xml = b'<rss><channel><item><title>Company news</title><guid>id1</guid><pubDate>Fri, 27 Nov 2026 15:00:00 +0800</pubDate></item></channel></rss>'
    class Feed:
        def raise_for_status(self): pass
        def iter_content(self, size): return iter([xml])
        def close(self): pass
    def get(url, **kwargs):
        assert url == 'https://feeds.feedburner.com/rsscna/finance'
        return Feed()
    transport = SimpleNamespace(session=SimpleNamespace(get=get))
    result = _execute_job(transport, {'provider':'cna','dataset':'finance'})
    assert len(result.records) == 1
    assert pd.Timestamp(result.records[0]['published_at']) == pd.Timestamp('2026-11-27T07:00:00Z')
    assert result.records[0]['available_at'] == result.retrieved_at


def test_invalid_cna_response_becomes_a_recoverable_source_error(monkeypatch, tmp_path):
    class InvalidFeed:
        def raise_for_status(self): pass
        def iter_content(self, size): return iter([b'<rss>'])
        def close(self): pass
    monkeypatch.setattr('requests.Session.get', lambda *args, **kwargs: InvalidFeed())
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps({'jobs':[dict(id='news',enabled=True,provider='cna',dataset='finance')]}))
    result = sync_sources(tmp_path, plan, Credentials({}))
    assert result['jobs'][0]['status'] == 'error'
