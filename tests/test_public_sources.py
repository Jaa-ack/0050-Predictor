import hashlib
import io
import json
import zipfile

import pandas as pd
import pytest

from twquant.sources import SourceResult,events_from_result


def test_official_disclosure_uses_speech_time_not_fact_date_and_never_backdates_receipt():
    from twquant.public_sources import normalize_announcements
    raw=[{'公司代號':'2330','公司名稱':'台積電','發言日期':'1151104','發言時間':'70004',
        '主旨 ':'公告說明','說明':'內容','事實發生日':'1151001','符合條款':'第51款'}]
    result=normalize_announcements(raw,'2026-11-05T00:00:00Z')
    row=result.records[0]
    assert row['published_at']=='2026-11-03T23:00:04+00:00'
    assert row['available_at']=='2026-11-05T00:00:00+00:00'
    assert row['ticker_ids']==['2330']
    assert row['title']=='公告說明'
    assert 'sentiment' not in row


def test_tdcc_preserves_zero_prefix_bom_keys_and_observed_availability():
    from twquant.public_sources import normalize_tdcc
    raw=[{'\ufeff資料日期':'20261030','證券代號':'0050  ','持股分級':'1','人數':'100','股數':'1000','占集保庫存數比例%':'0.25'},
         {'\ufeff資料日期':'20261030','證券代號':'2330  ','持股分級':'1','人數':'200','股數':'2000','占集保庫存數比例%':'0.4'}]
    result=normalize_tdcc(raw,'2026-11-05T00:00:00Z',['0050'])
    assert len(result.records)==1
    assert result.records[0]['ticker']=='0050'
    events=events_from_result(result)
    ratio=events[events.feature=='tdcc_band_1_ratio'].iloc[0]
    assert ratio.value==pytest.approx(.0025)
    assert ratio.available_at==pd.Timestamp('2026-11-05T00:00:00Z')
    assert ratio.period_end=='2026-10-30'
    assert ratio.availability_basis=='observed_retrieval_only'


def test_calendar_distinguishes_open_markers_holidays_and_unknown_events():
    from twquant.public_sources import normalize_calendar,calendar_candidate
    raw=[{'Date':'1150101','Name':'開國紀念日','Description':'依規定放假1日。'},
         {'Date':'1150102','Name':'國曆新年開始交易日','Description':'國曆新年開始交易。'},
         {'Date':'1150212','Name':'市場無交易，僅辦理結算交割作業','Description':''},
         {'Date':'1151009','Name':'國慶日','Description':'於10月9日（星期五）補假。'}]
    result=normalize_calendar(raw,'2026-11-05T00:00:00Z')
    candidate=calendar_candidate(result,2026)
    by_date=candidate.set_index('date')
    assert by_date.loc['2026-01-01','is_open']==0
    assert by_date.loc['2026-01-02','is_open']==1
    assert by_date.loc['2026-02-12','is_open']==0
    assert by_date.loc['2026-10-09','is_open']==0
    assert not candidate.verified.any()
    unknown=normalize_calendar([{'Date':'1150103','Name':'新型事件','Description':'待確認'}],'2026-11-05T00:00:00Z')
    with pytest.raises(ValueError,match='unknown|unclassified'):
        calendar_candidate(unknown,2026)


def gkg_zip():
    rows=[]
    for url,org in [('https://example.com/one','TSMC'),('https://example.com/two','Other company')]:
        row=['']*27
        row[0]='20261105020000-1';row[1]='20261105020000';row[4]=url;row[13]=org
        rows.append('\t'.join(row))
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('20261105020000.gkg.csv','\n'.join(rows))
    return out.getvalue()


def test_gdelt_archive_verifies_checksum_and_exports_metadata_without_invented_title():
    from twquant.public_sources import parse_gkg_archive
    payload=gkg_zip()
    result=parse_gkg_archive(payload,hashlib.md5(payload).hexdigest(),'20261105020000',
        '2026-11-05T03:00:00Z',terms=['tsmc'],max_records=10)
    assert len(result.records)==1
    row=result.records[0]
    assert row['url']=='https://example.com/one'
    assert row['title'] is None and row['published_at'] is None
    assert row['available_at']=='2026-11-05T03:00:00+00:00'
    assert result.metadata['coverage']=='one_latest_15_minute_batch_only'
    with pytest.raises(ValueError,match='checksum'):
        parse_gkg_archive(payload,'0'*32,'20261105020000','2026-11-05T03:00:00Z',terms=[])


def test_gdelt_download_rejects_redirects_and_bounds_bytes_before_parsing():
    from twquant.public_sources import _download
    from twquant.credentials import Credentials
    from twquant.sources import SourceTransport,SourceHTTPError
    class Response:
        status_code=302
        def iter_content(self,size):yield b'123456'
        def close(self):pass
    class Session:
        def request(self,*args,**kwargs):
            assert kwargs['allow_redirects'] is False
            assert 'Authorization' not in kwargs.get('headers',{})
            return response
    response=Response()
    t=SourceTransport(Credentials({}),session=Session())
    with pytest.raises(ValueError,match='HTTPS'):_download(t,'http://data.gdeltproject.org/gdeltv2/lastupdate.txt',10)
    with pytest.raises(SourceHTTPError):_download(t,'https://data.gdeltproject.org/gdeltv2/lastupdate.txt',10)
    response.status_code=200
    with pytest.raises(ValueError,match='size'):_download(t,'https://data.gdeltproject.org/gdeltv2/lastupdate.txt',5)


def test_gdelt_download_retries_one_transient_network_failure():
    import requests
    from twquant.public_sources import _download
    from twquant.credentials import Credentials
    from twquant.sources import SourceTransport
    class Response:
        status_code=200
        def iter_content(self,size):yield b'valid'
        def close(self):pass
    class Session:
        count=0
        def request(self,*args,**kwargs):
            self.count+=1
            if self.count==1:raise requests.Timeout('temporary')
            return Response()
    session=Session()
    t=SourceTransport(Credentials({}),session=session,sleeper=lambda _:None)
    assert _download(t,'https://data.gdeltproject.org/gdeltv2/lastupdate.txt',10)==b'valid'
    assert session.count==2


def test_documents_export_retains_first_receipt_and_later_content_revisions(tmp_path):
    from twquant.source_store import SourceStore
    store=SourceStore(tmp_path/'sources')
    base={'document_id':'same','title':'original','url':'https://example.com/a','published_at':'2026-11-04T00:00Z'}
    for stamp,title in [('2026-11-04T01:00Z','original'),('2026-11-04T02:00Z','original'),('2026-11-05T01:00Z','corrected'),('2026-11-06T01:00Z','original')]:
        row={**base,'title':title,'available_at':stamp}
        store.write(SourceResult('twse','announcements','news',[row],stamp))
    rows=store.export_documents(tmp_path/'documents.jsonl')
    assert len(rows)==3
    assert rows[0]['available_at']=='2026-11-04T01:00Z'
    assert rows[1]['title']=='corrected'
    assert rows[2]['title']=='original'


def test_unverified_calendar_cannot_be_activated_by_cli(tmp_path):
    from twquant.cli import execute,parser
    path=tmp_path/'candidate.csv'
    pd.DataFrame([dict(date='2026-01-02',is_open=1,source='official schedule candidate',verified=False)]).to_csv(path,index=False)
    args=parser().parse_args(['--root',str(tmp_path/'run'),'calendar','--csv',str(path)])
    with pytest.raises(ValueError,match='not verified'):
        execute(args)
    assert not (tmp_path/'run/session_calendar.csv').exists()


def test_malformed_gdelt_zip_is_a_controlled_data_error():
    from twquant.public_sources import parse_gkg_archive
    payload=b'not a zip'
    with pytest.raises(ValueError,match='archive'):
        parse_gkg_archive(payload,hashlib.md5(payload).hexdigest(),'20261105020000','2026-11-05T03:00:00Z')
