import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from twquant.credentials import Credentials
from twquant.sources import (SourceResult,SourceTransport,TwinkleClient,events_from_result,
    fetch_fred,fetch_fmp,fetch_fugle_prices,fetch_fugle,fetch_news,fetch_fubon_prices,fetch_public,holdings_from_result)


class Response:
    def __init__(self,payload,status=200,headers=None,text=None):
        self.payload=payload
        self.status_code=status
        self.headers=headers or {}
        self.text=text if text is not None else json.dumps(payload)
    def json(self):
        return self.payload


class Session:
    def __init__(self,handler):
        self.handler=handler
        self.calls=[]
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        return self.handler(method,url,kwargs)


def transport(handler,tmp_path=None):
    creds=Credentials({k:'example-secret-'+k for k in ['FRED_API_KEY','FMP_API_KEY','NEWS_API_KEY','TWINKLE_API_KEY','FUGLE_API_KEY']})
    return SourceTransport(creds,raw_dir=tmp_path,session=Session(handler),min_intervals={'fugle':0})


def test_fugle_requests_are_paced_to_the_documented_sixty_per_minute_budget():
    state={'now':0.,'sleeps':[]}
    def sleep(seconds):
        state['sleeps'].append(seconds)
        state['now']+=seconds
    t=SourceTransport(Credentials({}),session=Session(lambda *a:Response({})),
                      clock=lambda:state['now'],sleeper=sleep)
    for _ in range(3):
        t.request('fugle','GET','https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/2330')
    assert len(state['sleeps'])==2
    assert all(seconds>=1. for seconds in state['sleeps'])


def test_transport_redacts_query_credentials_echo_and_rejects_redirects(tmp_path):
    t=transport(lambda *a:Response({'echo':'example-secret-FRED_API_KEY'}),tmp_path)
    t.request('fred','GET','https://api.stlouisfed.org/fred/series/observations',params={'api_key':'example-secret-FRED_API_KEY'})
    assert all('example-secret' not in p.read_text() for p in tmp_path.glob('*.json'))
    assert t.session.calls[0][2]['allow_redirects'] is False
    with pytest.raises(ValueError,match='host|HTTPS'):
        t.request('fred','GET','http://127.0.0.1/internal')
    bad=transport(lambda *a:Response({},302,{'Location':'http://private/'}))
    with pytest.raises(RuntimeError,match='302'):
        bad.request('fred','GET','https://api.stlouisfed.org/fred/series/observations')


def test_network_exception_never_echoes_secret_url_or_traceback_cause():
    def handler(*a):
        raise requests.ConnectionError('https://host/?api_key=example-secret-FRED_API_KEY')
    t=transport(handler)
    with pytest.raises(RuntimeError) as e:
        t.request('fred','GET','https://api.stlouisfed.org/fred/series/observations')
    assert 'example-secret' not in str(e.value)
    assert e.value.__suppress_context__


def test_twinkle_sse_uses_utf8_and_only_protocol_newlines():
    payload={'jsonrpc':'2.0','id':1,'result':{'instructions':'政府資料\u0085不是行分隔符'}}
    class SSE:
        status_code=200
        headers={'Content-Type':'text/event-stream'}
        encoding='ISO-8859-1'
        @property
        def text(self):
            body='event: message\r\ndata: '+json.dumps(payload,ensure_ascii=False)+'\r\n\r\n'
            return body.encode('utf-8').decode(self.encoding)
    t=transport(lambda *a:SSE())
    result=t.request('twinkle','POST','https://api.twinkleai.tw/mcp/',json={'id':1})
    assert result==payload


def test_twinkle_notification_accepts_empty_202_without_weakening_rpc_validation():
    class Empty(Response):
        def json(self):raise ValueError('empty')
    t=transport(lambda *a:Empty(None,202,text=''))
    assert t.request('twinkle','POST','https://api.twinkleai.tw/mcp/',json={'method':'notifications/initialized'})=={}


def test_official_futures_csv_is_normalized_with_strict_headers():
    csv='日期,契約代號,到期月份(週別),最後成交價,交易時段\r\n20261104,TX,202611,30000,一般\r\n'
    t=transport(lambda *a:Response(None,headers={'Content-Type':'application/octet-stream'},text=csv))
    result=fetch_public(t,'taifex','futures')
    assert result.records==[{'日期':'20261104','契約代號':'TX','到期月份(週別)':'202611','最後成交價':'30000','交易時段':'一般'}]


def test_non_json_entitlement_error_retains_http_status_without_echoing_body():
    from twquant.sources import SourceHTTPError
    class NonJson(Response):
        def json(self):raise ValueError('not JSON')
    t=transport(lambda *a:NonJson(None,402,text='Premium query example-secret-FMP_API_KEY'))
    with pytest.raises(SourceHTTPError) as exc:
        fetch_fmp(t,'adjusted_prices','QQQ','2026-10-01','2026-10-02')
    assert exc.value.status_code==402
    assert 'example-secret' not in str(exc.value)


def test_fmp_financial_probe_defaults_to_basic_plan_five_records_and_discloses_cap():
    def handler(m,u,k):
        if k['params']['limit']>5:
            return Response({'Error Message':'limit exceeds subscription'},402)
        return Response([{'symbol':'TSM','date':'2026-06-30','eps':2}])
    result=fetch_fmp(transport(handler),'income','TSM','2026-01-01','2026-11-04')
    assert len(result.records)==1
    assert result.metadata['record_limit']==5
    assert result.metadata['complete'] is False


def test_fred_downloads_vintages_with_pagination_and_does_not_date_by_period():
    def handler(m,u,k):
        offset=k['params'].get('offset',0)
        return Response({'count':2,'observations':[{'date':'2024-01-01','realtime_start':f'2024-0{2+offset}-10','value':str(100+offset)}]})
    t=transport(handler)
    result=fetch_fred(t,'CPIAUCSL','2024-01-01','2024-04-01')
    assert len(result.records)==2
    assert t.session.calls[0][2]['params']['realtime_start']=='2024-01-01'
    events=events_from_result(result)
    assert len(events)==2
    assert events.published_at.min()>pd.Timestamp('2024-02-10T00:00:00Z')
    assert events.period_end.iloc[0]=='2024-01-01'


def test_fred_long_daily_history_bounds_vintage_windows_and_resets_page_offsets():
    def handler(m,u,k):
        p=k['params']
        span=(pd.Timestamp(p['realtime_end'])-pd.Timestamp(p['realtime_start'])).days
        if span>=2000:
            return Response({'error_message':'Too many vintage dates'},400)
        offset=p['offset']
        day=(pd.Timestamp(p['realtime_start'])+pd.Timedelta(days=offset)).date().isoformat()
        return Response({'count':2,'observations':[{'date':day,'realtime_start':day,'value':str(offset+1)}]})
    t=transport(handler)
    result=fetch_fred(t,'DGS10','2014-01-01','2026-11-04')
    assert len(result.records)>2
    params=[call[2]['params'] for call in t.session.calls]
    assert params[0]['realtime_start']=='2014-01-01'
    assert params[-1]['realtime_end']=='2026-11-04'
    windows=list(dict.fromkeys((p['realtime_start'],p['realtime_end']) for p in params))
    for earlier,later in zip(windows,windows[1:]):
        assert pd.Timestamp(earlier[1])+pd.Timedelta(days=1)==pd.Timestamp(later[0])
    assert [p['offset'] for p in params]==[0,1]*len(windows)
    assert set(events_from_result(result).availability_basis)=={'vintage_day_conservative'}


def test_fred_old_period_revision_never_replaces_newest_known_period():
    result=SourceResult('fred','CPIAUCSL','macro',[
        {'date':'2024-01-01','realtime_start':'2024-02-10','value':'100'},
        {'date':'2024-02-01','realtime_start':'2024-03-10','value':'102'},
        {'date':'2024-01-01','realtime_start':'2024-04-10','value':'101'}], '2025-01-01T00:00:00Z')
    events=events_from_result(result)
    assert events.iloc[-1].period_end=='2024-02-01'
    assert events.iloc[-1].value==102


def test_fred_future_dated_observation_cannot_become_available_before_its_period():
    result=SourceResult('fred','VIXCLS','macro',[
        {'date':'2026-01-16','realtime_start':'2026-01-16','value':'18'},
        {'date':'2026-01-19','realtime_start':'2026-01-16','value':'18.84'}], '2026-11-05T00:00:00Z')
    events=events_from_result(result)
    assert len(events)==2
    assert events.iloc[-1].period_end=='2026-01-19'
    assert events.iloc[-1].available_at==pd.Timestamp('2026-01-20T05:00:00Z')
    assert events.iloc[0].available_at<events.iloc[-1].available_at


def candle(symbol='0050',adjusted=False,date='2025-01-02'):
    return {'symbol':symbol,'timeframe':'D','adjusted':adjusted,'data':[
        {'date':date,'open':50,'high':53,'low':49,'close':26 if adjusted else 52,'volume':1000}]}


def test_fugle_pairs_raw_adjusted_and_splits_long_windows():
    def handler(m,u,k):
        p=k['params']
        assert (pd.Timestamp(p['to'])-pd.Timestamp(p['from'])).days<365
        return Response(candle(adjusted=p['adjusted']=='true',date=p['from']))
    t=transport(handler)
    p=fetch_fugle_prices(t,['0050'],'2024-01-01','2025-02-01')
    assert len(t.session.calls)==4
    assert p.close.iloc[0]==52
    assert p.adj_close.iloc[0]==26
    assert set(p.availability_basis)=={'reconstructed_eod'}


def test_fugle_rejects_silent_unsupported_adjusted_request():
    t=transport(lambda *a:Response(candle(adjusted=False)))
    with pytest.raises(RuntimeError,match='adjust'):
        fetch_fugle_prices(t,['0050'],'2025-01-02','2025-01-03')


def test_fugle_pre_ipo_404_windows_are_skipped_without_losing_later_history():
    def handler(m,u,k):
        p=k['params']
        if p['from']<'2025-01-01':
            return Response({'message':'Resource Not Found'},404)
        return Response(candle('3711',adjusted=p['adjusted']=='true',date=p['from']))
    t=transport(handler)
    frame=fetch_fugle_prices(t,['3711'],'2024-02-01','2025-02-01')
    assert len(frame)==1
    assert frame.date.min()>=pd.Timestamp('2025-01-01')


def test_missing_requested_instrument_is_an_error_even_when_other_ticker_exists():
    def handler(m,u,k):
        if u.endswith('/BAD'):
            return Response({'message':'Resource Not Found'},404)
        return Response(candle(adjusted=k['params']['adjusted']=='true'))
    with pytest.raises(RuntimeError,match='BAD'):
        fetch_fugle_prices(transport(handler),['0050','BAD'],'2025-01-02','2025-01-03')


def test_membership_converts_percent_preserves_effective_date_and_known_time():
    result=SourceResult('fugle','holdings','membership',[
        {'date':'2025-01-02','components':[{'symbol':'2330','name':'台積電','weight':57.1,'quantity':100}]}],
        '2026-11-04T08:00:00Z',{'symbol':'0050'})
    m=holdings_from_result(result)
    assert m.iloc[0].weight==pytest.approx(.571)
    assert m.iloc[0].ticker=='2330'
    assert pd.Timestamp(m.iloc[0].available_at)>pd.Timestamp('2025-01-02T00:00:00Z')
    assert not m.iloc[0].historical_announcement_verified


def test_fmp_us_market_timestamps_follow_dst_and_never_use_today_as_historical_publication():
    def handler(m,u,k):
        return Response([{'date':'2025-01-03','close':100},{'date':'2025-07-03','close':110}])
    result=fetch_fmp(transport(handler),'prices','SPY','2025-01-01','2025-08-01')
    e=events_from_result(result)
    assert e.available_at.iloc[0]==pd.Timestamp('2025-01-03T22:00:00Z')
    assert e.available_at.iloc[1]==pd.Timestamp('2025-07-03T21:00:00Z')
    assert set(e.availability_basis)=={'reconstructed_us_close_plus_one_hour'}


def test_news_paginates_deduplicates_and_uses_actual_retrieval_time():
    article={'title':'TSMC report','url':'https://example.com/a','publishedAt':'2025-01-02T01:00:00Z','source':{'id':'demo'}}
    result=fetch_news(transport(lambda *a:Response({'status':'ok','totalResults':2,'articles':[article,article]})),
                      'TSMC','2025-01-01','2025-01-03',tickers=['2330'])
    assert len(result.records)==1
    assert pd.Timestamp(result.records[0]['available_at'])>=pd.Timestamp(result.records[0]['published_at'])
    assert result.metadata['sentiment_model']=='not_applied'


def test_twinkle_sse_handshake_and_nested_execute_tool_envelope():
    def handler(m,u,k):
        body=k['json']
        if body['method']=='initialize':
            return Response({'jsonrpc':'2.0','id':body['id'],'result':{'protocolVersion':'2025-03-26'}},headers={'mcp-session-id':'secret-session'})
        if body['method'].startswith('notifications/'):
            return Response({},204)
        return Response(None,headers={'Content-Type':'text/event-stream'},text='event: message\ndata: '+json.dumps({'jsonrpc':'2.0','id':body['id'],'result':{'content':[{'type':'text','text':'{"rows": []}'}]}})+'\n\n')
    t=transport(handler)
    result=TwinkleClient(t).call('tw_search_datasets',{'query':'出口','limit':5})
    call=t.session.calls[-1][2]
    assert call['headers']['mcp-session-id']=='secret-session'
    assert call['json']['params']=={'name':'execute_tool','arguments':{'tool_id':'tw_search_datasets','arguments':{'query':'出口','limit':5}}}
    assert result.provider=='twinkle'


def test_fubon_calls_only_market_data_read_methods():
    calls=[]
    def candles(**kwargs):
        calls.append(kwargs)
        return candle(adjusted=kwargs['adjusted']=='true')
    sdk=SimpleNamespace(marketdata=SimpleNamespace(rest_client=SimpleNamespace(stock=SimpleNamespace(historical=SimpleNamespace(candles=candles)))))
    p=fetch_fubon_prices(transport(lambda *a:None),['0050'],'2025-01-02','2025-01-03',sdk=sdk)
    assert len(calls)==2
    assert set(p.source)=={'fubon'}
    assert p.adj_close.iloc[0]==26


def test_public_sources_have_fixed_documented_routes_without_credentials():
    t=transport(lambda *a:Response([{'Code':'2330','ClosingPrice':'100'}]))
    result=fetch_public(t,'twse','prices')
    assert result.records[0]['Code']=='2330'
    assert t.session.calls[-1][1]=='https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'
    assert not t.session.calls[-1][2].get('headers',{}).get('Authorization')
    with pytest.raises(ValueError):
        fetch_public(t,'twse','../../etc/passwd')


def test_financial_snapshots_are_not_backdated_to_original_filing():
    result=SourceResult('finmind','TaiwanStockFinancialStatements','financial',[
        {'stock_id':'2330','date':'2024-03-31','type':'EPS','value':10},
        {'stock_id':'2330','date':'2024-06-30','type':'EPS','value':12}],
        '2026-11-04T12:00:00Z',{'symbol':'2330'})
    events=events_from_result(result)
    assert len(events)==1
    assert events.iloc[0].value==12
    assert events.iloc[0].available_at==pd.Timestamp('2026-11-04T12:00:00Z')
    assert events.iloc[0].availability_basis=='observed_retrieval_only'


def test_institutional_categories_are_not_collapsed_into_last_investor():
    result=SourceResult('finmind','TaiwanStockInstitutionalInvestorsBuySell','market',[
        {'stock_id':'2330','date':'2025-01-02','name':'Foreign_Investor','buy':100,'sell':60},
        {'stock_id':'2330','date':'2025-01-02','name':'Investment_Trust','buy':70,'sell':80}],
        '2026-11-04T12:00:00Z',{'symbol':'2330'})
    e=events_from_result(result)
    assert len(e)==6
    assert e[e.feature.str.contains('Foreign_Investor_net')].value.iloc[0]==40
    assert e[e.feature.str.contains('Investment_Trust_net')].value.iloc[0]==-10


def test_fmp_adjusted_endpoint_and_explicit_adjusted_close_mapping():
    t=transport(lambda *a:Response([{'date':'2025-01-02','adjClose':100}]))
    result=fetch_fmp(t,'adjusted_prices','SPY','2025-01-01','2025-01-03')
    assert t.session.calls[-1][1].endswith('/historical-price-eod/dividend-adjusted')
    assert events_from_result(result).iloc[0].value==100


def test_fmp_rejects_wrong_symbol_or_out_of_range_prices():
    t=transport(lambda *a:Response([{'date':'2026-01-02','symbol':'WRONG','close':100}]))
    with pytest.raises(RuntimeError,match='range|instrument'):
        fetch_fmp(t,'prices','SPY','2025-01-01','2025-01-03')


def test_twinkle_cannot_call_mutating_tools():
    with pytest.raises(ValueError,match='read-only'):
        TwinkleClient(transport(lambda *a:None)).call('tw_send_email',{})


def test_fred_incomplete_page_is_an_error_instead_of_silent_truncation():
    t=transport(lambda *a:Response({'count':10,'observations':[]}))
    with pytest.raises(RuntimeError,match='pagination'):
        fetch_fred(t,'GDP','2024-01-01','2025-01-01')


def test_fubon_api_key_login_uses_certificate_without_account_password(tmp_path,monkeypatch):
    import sys
    import types
    certificate=tmp_path/'cert.pfx'
    certificate.write_bytes(b'example-only-not-certificate')
    certificate.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    log_dir=tmp_path/'log';log_dir.mkdir(mode=0o755)
    log_file=log_dir/'sdk.20261105';log_file.write_text('private-sdk-record');log_file.chmod(0o644)
    calls=[]
    class SDK:
        def __init__(self):
            assert log_dir.stat().st_mode&0o777==0o700
            assert log_file.stat().st_mode&0o777==0o600
        def apikey_login(self,*args):
            calls.append(('apikey_login',len(args)))
            return SimpleNamespace(is_success=True)
        def init_realtime(self):
            self.marketdata=SimpleNamespace(rest_client=SimpleNamespace(stock=SimpleNamespace(
                historical=SimpleNamespace(candles=lambda **k:candle(adjusted=k['adjusted']=='true')))))
    module=types.ModuleType('fubon_neo.sdk')
    module.FubonSDK=SDK
    monkeypatch.setitem(sys.modules,'fubon_neo',types.ModuleType('fubon_neo'))
    monkeypatch.setitem(sys.modules,'fubon_neo.sdk',module)
    c=Credentials({'FUBON_API_KEY':'example-api-key','FUBON_PERSONAL_ID':'example-id',
                   'FUBON_CERT_PATH':str(certificate),'FUBON_CERT_PASSWORD':'example-cert-password'})
    t=SourceTransport(c,raw_dir=tmp_path/'raw',session=Session(lambda *a:None))
    prices=fetch_fubon_prices(t,['0050'],'2025-01-02','2025-01-03')
    assert len(prices)==1
    assert calls==[('apikey_login',4)]
    assert all('example-api-key' not in p.read_text() and 'example-id' not in p.read_text() for p in (tmp_path/'raw').glob('*.json'))


def test_fred_today_uses_provider_day_before_us_midnight(monkeypatch):
    import twquant.sources as module
    monkeypatch.setattr(module,'now_utc',lambda:'2026-11-05T02:00:00Z')
    requested=[]
    def fetch(method,url,kwargs):
        params=kwargs['params'];requested.append(params)
        if params['realtime_end']>'2026-11-04':
            return Response({'error_code':400},400)
        return Response({'count':1,'observations':[{'date':'2026-11-03','realtime_start':'2026-11-04','value':'4.0'}]})
    result=fetch_fred(transport(fetch),'DGS10','2026-11-01','2026-11-05')
    assert result.records
    assert requested[-1]['realtime_end']=='2026-11-04'
    assert result.metadata['requested_end']=='2026-11-05'


def test_index_prices_preserve_price_index_basis_instead_of_stock_total_return():
    def handler(m,u,k):
        result=candle('IX0001',adjusted=k['params']['adjusted']=='true')
        result['type']='INDEX'
        return Response(result)
    frame=fetch_fugle_prices(transport(handler),['IX0001'],'2025-01-02','2025-01-03')
    assert set(frame.asset_class)=={'index'}
    assert set(frame.return_basis)=={'price_index'}
    assert set(frame.adjustment_source)=={'fugle_price_index_no_dividend_reinvestment'}
