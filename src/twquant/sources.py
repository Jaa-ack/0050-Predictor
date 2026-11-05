"""Read-only source adapters. Network acquisition and model-ready normalization are separate."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse
import uuid

import numpy as np
import pandas as pd
import requests

from .credentials import redact,install_secret_filters
from .data import validate_prices

HOSTS={"fred":"api.stlouisfed.org","fmp":"financialmodelingprep.com","newsapi":"newsapi.org",
       "twinkle":"api.twinkleai.tw","fugle":"api.fugle.tw","finmind":"api.finmindtrade.com",
       "twse":"openapi.twse.com.tw","taifex":"openapi.taifex.com.tw","tdcc":"openapi.tdcc.com.tw"}
PUBLIC_ROUTES={
    ("twse","prices"):"https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    ("twse","companies"):"https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    ("twse","revenue"):"https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    ("taifex","futures"):"https://openapi.taifex.com.tw/v1/DailyMarketReportFut",
    ('twse','announcements'):'https://openapi.twse.com.tw/v1/opendata/t187ap04_L',
    ('twse','calendar'):'https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule',
}


class SourceHTTPError(RuntimeError):
    def __init__(self,provider,status_code):
        self.provider=provider
        self.status_code=status_code
        super().__init__(f"{provider}: HTTP {status_code}; check entitlement, quota and parameters")

FINMIND_DATASETS={"TaiwanStockPrice","TaiwanStockPriceAdj","TaiwanStockInstitutionalInvestorsBuySell",
    "TaiwanStockMarginPurchaseShortSale","TaiwanStockSecuritiesLending","TaiwanStockFinancialStatements",
    "TaiwanStockBalanceSheet","TaiwanStockCashFlowsStatement","TaiwanStockMonthRevenue",
    "TaiwanStockDividend","TaiwanStockDividendResult","TaiwanStockPER","TaiwanFuturesDaily",
    "TaiwanOptionDaily","USStockPrice","TaiwanStockShareholding","TaiwanStockHoldingSharesPer"}


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9^_.!\-]{1,40}",value):
        raise ValueError("Invalid source instrument identifier")
    return value


def date_windows(start,end,days=360):
    first,last=pd.Timestamp(start),pd.Timestamp(end)
    if first.tzinfo or last.tzinfo or first>last:
        raise ValueError("start/end require ordered daily dates")
    while first<=last:
        stop=min(first+pd.Timedelta(days=days-1),last)
        yield first.date().isoformat(),stop.date().isoformat()
        first=stop+pd.Timedelta(days=1)


@dataclass
class SourceResult:
    provider: str
    dataset: str
    kind: str
    records: list
    retrieved_at: str
    metadata: dict = field(default_factory=dict)


class SourceTransport:
    def __init__(self, credentials, raw_dir=None, session=None,min_intervals=None,clock=None,sleeper=None):
        self.credentials=credentials
        install_secret_filters(credentials)
        self.raw_dir=raw_dir
        self.session=session or requests.Session()
        self.last_headers={}
        self.retrieved_at=now_utc()
        self.min_intervals={'fugle':1.05,**(min_intervals or {})}
        self.clock=clock or time.monotonic
        self.sleeper=sleeper or time.sleep
        self.last_request={}

    def archive(self,provider,dataset,params,payload):
        if self.raw_dir is None:
            return
        folder=Path(self.raw_dir)
        folder.mkdir(parents=True,exist_ok=True)
        document=redact(dict(provider=provider,dataset=dataset,params=params,response=payload,
                             retrieved_at=self.retrieved_at),self.credentials.secret_values())
        body=json.dumps(document,ensure_ascii=False,allow_nan=False,default=str).encode()
        digest=hashlib.sha256(body).hexdigest()
        name=f"{provider}_{uuid.uuid4().hex}"
        with (folder/(name+".json")).open("xb") as handle:
            handle.write(body)
        (folder/(name+".sha256")).write_text(digest+"\n")

    def request(self, provider, method, url, **kwargs):
        parsed=urlparse(url)
        if (provider not in HOSTS or parsed.scheme!="https" or parsed.hostname!=HOSTS[provider]
            or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.query or parsed.fragment):
            raise ValueError("Outbound source requires its allowlisted HTTPS host; pass queries as structured params")
        if method not in {"GET","POST"} or (method=="POST" and provider!="twinkle"):
            raise ValueError("Only documented read-only source requests are supported")
        # No redirects: provider query keys and bearer headers may not travel to another host.
        attempts=3 if method=="GET" else 1
        response=None
        for attempt in range(attempts):
            interval=self.min_intervals.get(provider,0.)
            remaining=interval-(self.clock()-self.last_request[provider]) if provider in self.last_request else 0.
            if remaining>0:
                self.sleeper(remaining)
            self.last_request[provider]=self.clock()
            try:
                response=self.session.request(method,url,timeout=(10,40),allow_redirects=False,**kwargs)
            except requests.RequestException:
                if attempt+1<attempts:
                    time.sleep(.1*2**attempt)
                    continue
                raise RuntimeError(f"{provider}: network request failed; credentials and request URL omitted") from None
            if response.status_code>=500 and attempt+1<attempts:
                time.sleep(.1*2**attempt)
                continue
            break
        self.retrieved_at=now_utc()
        self.last_headers=dict(response.headers)
        if response.status_code==204 or (provider=='twinkle' and response.status_code==202 and
            kwargs.get('json',{}).get('method','').startswith('notifications/')):
            return {}
        try:
            if "text/event-stream" in response.headers.get("Content-Type",""):
                response.encoding='utf-8'  # SSE is UTF-8, despite requests' text/* Latin-1 default.
                messages=[]
                for block in response.text.replace("\r\n","\n").split("\n\n"):
                    data="\n".join(line[5:].lstrip() for line in block.split('\n') if line.startswith("data:"))
                    if data:
                        messages.append(json.loads(data))
                expected=kwargs.get("json",{}).get("id")
                matching=[m for m in messages if m.get("id")==expected]
                if not matching:
                    raise ValueError("No matching MCP response")
                payload=matching[-1]
            elif (provider=='taifex' and parsed.path=='/v1/DailyMarketReportFut' and response.status_code==200
                  and not response.text.lstrip().startswith(('[','{'))):
                body=getattr(response,'content',None)
                if body is not None:
                    try:
                        body=body.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        body=body.decode('cp950')
                else:
                    body=response.text.lstrip('\ufeff')
                reader=csv.DictReader(io.StringIO(body))
                required={'日期','契約代號','到期月份(週別)','最後成交價','交易時段'}
                if not required<=set(reader.fieldnames or []):
                    raise ValueError('Official futures CSV schema mismatch')
                payload=list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in payload):
                    raise ValueError('Malformed official futures CSV row')
            else:
                payload=response.json()
        except (ValueError,TypeError):
            if response.status_code!=200:
                self.archive(provider,parsed.path,{},dict(http_status=response.status_code,response_format='unparsed'))
                raise SourceHTTPError(provider,response.status_code) from None
            raise RuntimeError(f"{provider}: invalid response (HTTP {response.status_code}); payload omitted") from None
        self.archive(provider,parsed.path,kwargs.get("params",kwargs.get("json",{})),payload)
        if response.status_code!=200:
            raise SourceHTTPError(provider,response.status_code) from None
        if isinstance(payload,dict) and (payload.get("status")=="error" or "Error Message" in payload or
                                        isinstance(payload.get("status"),int) and payload["status"]!=200):
            raise RuntimeError(f"{provider}: provider rejected request; check access and parameters") from None
        return payload


def _result(t,provider,dataset,kind,records,**metadata):
    if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
        raise RuntimeError(f"{provider}/{dataset}: response schema mismatch")
    return SourceResult(provider,dataset,kind,redact(records,t.credentials.secret_values()),t.retrieved_at,metadata)


def fetch_fred(transport, series_id, start, end):
    identifier(series_id)
    list(date_windows(start,end))
    records=[]
    # JSON permits at most 2,000 vintage dates. Bound real-time windows too,
    # not only observation dates; daily series can otherwise fail even for one month.
    for first,last in date_windows(start,end,days=1460):
        offset=0
        for _ in range(100):
            params=dict(series_id=series_id,api_key=transport.credentials.get("FRED_API_KEY").get_secret_value(),
                file_type="json",observation_start=start,observation_end=end,realtime_start=first,
                realtime_end=last,output_type=1,sort_order="asc",limit=100000,offset=offset)
            payload=transport.request("fred","GET","https://api.stlouisfed.org/fred/series/observations",params=params)
            batch=payload.get("observations")
            if not isinstance(batch,list):
                raise RuntimeError("fred: missing observations")
            records.extend(batch)
            offset+=len(batch)
            if offset>=int(payload.get("count",offset)):
                break
            if not batch:
                raise RuntimeError("fred: incomplete pagination")
        else:
            raise RuntimeError("fred: pagination limit reached; split the date range")
    return _result(transport,"fred",series_id,"macro",records,vintage_mode="all_real_time_periods",
        release_time_precision="date_only",complete=True)


def fetch_fmp(transport, dataset, symbol, start, end,statement_limit=5,period='quarter'):
    routes={"prices":"historical-price-eod/full","adjusted_prices":"historical-price-eod/dividend-adjusted",
            "income":"income-statement","balance":"balance-sheet-statement",
            "cashflow":"cash-flow-statement","estimates":"analyst-estimates","news":"news/stock"}
    if dataset not in routes:
        raise ValueError("Unsupported FMP dataset")
    identifier(symbol)
    if isinstance(statement_limit,bool) or not isinstance(statement_limit,int) or not 1<=statement_limit<=1000 or period not in {'quarter','annual'}:
        raise ValueError('FMP financial limit must be 1..1000 and period quarter/annual')
    params=dict(symbol=symbol,apikey=transport.credentials.get("FMP_API_KEY").get_secret_value())
    if dataset in {"prices","adjusted_prices","news"}:
        params.update({"from":start,"to":end})
    else:
        params.update(period=period,limit=statement_limit)
    payload=transport.request("fmp","GET","https://financialmodelingprep.com/stable/"+routes[dataset],params=params)
    if isinstance(payload,dict) and "historical" in payload:
        payload=payload["historical"]
    if dataset in {'prices','adjusted_prices'}:
        if not isinstance(payload,list):
            raise RuntimeError('fmp: price response schema mismatch')
        for record in payload:
            if record.get('symbol',symbol)!=symbol:
                raise RuntimeError('fmp: returned instrument mismatch')
            if not record.get('date') or not pd.Timestamp(start)<=pd.Timestamp(record['date'])<=pd.Timestamp(end):
                raise RuntimeError('fmp: price outside requested date range')
    kind="market" if dataset in {"prices","adjusted_prices"} else "news" if dataset=="news" else "financial"
    return _result(transport,"fmp",dataset,kind,payload,symbol=symbol,
        complete=dataset in {"prices","adjusted_prices"},historical_revision_verified=False,
        record_limit=statement_limit if kind=='financial' else None,financial_period=period if kind=='financial' else None,
        adjustment_source=routes[dataset] if kind=='market' else None,
        limitation="Statements/estimates are capped current vendor snapshots, not complete historical analyst vintages; news requires dedicated pagination before claiming complete history.")


def fetch_finmind(transport,dataset,symbol,start,end):
    if dataset not in FINMIND_DATASETS:
        raise ValueError("Unsupported FinMind dataset")
    params=dict(dataset=dataset,data_id=identifier(symbol),start_date=start,end_date=end)
    headers={}
    if transport.credentials.status()["FINMIND_TOKEN"]:
        headers["Authorization"]="Bearer "+transport.credentials.get("FINMIND_TOKEN").get_secret_value()
    payload=transport.request("finmind","GET","https://api.finmindtrade.com/api/v4/data",params=params,headers=headers)
    return _result(transport,"finmind",dataset,"financial" if dataset in {"TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet","TaiwanStockCashFlowsStatement","TaiwanStockMonthRevenue"} else "market",
        payload.get("data"),symbol=symbol,availability_basis="observed_retrieval_only",
        historical_revision_verified=False)


def _paired_prices(provider,tickers,start,end,get):
    frames=[]
    for ticker in dict.fromkeys(tickers):
        identifier(ticker)
        before=len(frames)
        for first,last in date_windows(start,end):
            results=[]
            for adjusted in (False,True):
                try:
                    results.append(get(ticker,first,last,adjusted))
                except SourceHTTPError as exc:
                    if exc.status_code!=404:
                        raise
                    results.append(None)
            raw,adj=results
            if raw is None and adj is None:
                # Documented no-data response: pre-IPO or no-session windows.
                # Never synthesize prices for dates before a security existed.
                continue
            if raw is None or adj is None:
                raise RuntimeError(f'{provider}/{ticker}: raw/adjusted window alignment incomplete')
            if not isinstance(raw,dict) or not isinstance(adj,dict) or adj.get("adjusted") is not True:
                raise RuntimeError(f"{provider}: adjusted price response not confirmed; require supported adjusted endpoint/SDK")
            if raw.get("symbol")!=ticker or adj.get("symbol")!=ticker or raw.get("timeframe")!="D" or adj.get("timeframe")!="D":
                raise RuntimeError(f"{provider}: candle instrument/timeframe mismatch")
            a,b=pd.DataFrame(raw.get("data",[])),pd.DataFrame(adj.get("data",[]))
            if a.empty and b.empty:
                continue
            if not {"date","open","high","low","close","volume"}<=set(a) or not {"date","close"}<=set(b):
                raise RuntimeError(f"{provider}: candle schema mismatch")
            if a.date.duplicated().any() or b.date.duplicated().any() or set(a.date)!=set(b.date):
                raise RuntimeError(f"{provider}: raw/adjusted date alignment incomplete")
            frame=a.merge(b[["date","close"]].rename(columns={"close":"adj_close"}),on="date",validate="one_to_one")
            frame["date"]=pd.to_datetime(frame.date)
            if frame.date.lt(pd.Timestamp(first)).any() or frame.date.gt(pd.Timestamp(last)).any():
                raise RuntimeError(f"{provider}: returned price outside requested range")
            frame["ticker"]=ticker
            frame["available_at"]=(frame.date.dt.tz_localize("Asia/Taipei")+pd.Timedelta(hours=20)).dt.tz_convert("UTC")
            frame["availability_basis"]="reconstructed_eod"
            frame["source"]=provider
            frame["adjustment_source"]=provider+"_latest_backward_adjusted"
            frame["retrieved_at"]=now_utc()
            frames.append(frame[["ticker","date","open","high","low","close","adj_close","volume",
                                  "available_at","availability_basis","source","adjustment_source","retrieved_at"]])
        if len(frames)==before:
            raise RuntimeError(f'{provider}: no usable adjusted daily prices for requested instrument {ticker}')
    if not frames:
        raise RuntimeError(f"{provider}: no usable adjusted daily prices")
    return validate_prices(pd.concat(frames,ignore_index=True))


def fetch_fugle_prices(transport, tickers, start, end):
    key=transport.credentials.get("FUGLE_API_KEY").get_secret_value()
    def get(ticker,first,last,adjusted):
        return transport.request("fugle","GET","https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/"+ticker,
            params={"from":first,"to":last,"timeframe":"D","adjusted":str(adjusted).lower(),
                    "sort":"asc","fields":"open,high,low,close,volume"},headers={"X-API-KEY":key})
    return _paired_prices("fugle",tickers,start,end,get)


def fetch_fugle(transport, dataset, symbol, start, end):
    routes={"holdings":"ownership/etf-holdings/","institutional":"ownership/institutional-trades/",
            "tdcc":"ownership/tdcc-distribution/","quote":"intraday/quote/","trades":"intraday/trades/",
            "candles":"intraday/candles/"}
    if dataset not in routes:
        raise ValueError("Unsupported Fugle dataset")
    key=transport.credentials.get("FUGLE_API_KEY").get_secret_value()
    records=[]
    missing_ranges=[]
    windows=list(date_windows(start,end)) if dataset in {"holdings","institutional","tdcc"} else [(None,None)]
    for first,last in windows:
        try:
            payload=transport.request("fugle","GET","https://api.fugle.tw/marketdata/v1.0/stock/"+routes[dataset]+identifier(symbol),
                params={"from":first,"to":last,"sort":"asc"} if first else {},headers={"X-API-KEY":key})
        except SourceHTTPError as exc:
            if exc.status_code!=404 or first is None:
                raise
            missing_ranges.append(dict(start=first,end=last))
            continue
        if payload.get("symbol")!=symbol:
            raise RuntimeError("fugle: returned instrument mismatch")
        batch=payload.get("data",[payload])
        records.extend(batch)
    return _result(transport,"fugle",dataset,"membership" if dataset=="holdings" else "market",records,
                   symbol=symbol,availability_basis="observed_retrieval_only",complete=not missing_ranges,
                   missing_ranges=missing_ranges)


def fetch_news(transport, query, start, end, tickers=(), max_pages=10):
    if not query or len(query)>500 or not 1<=max_pages<=100:
        raise ValueError("News query/page limit invalid")
    records=[]
    seen=set()
    received=0
    total=0
    for page in range(1,max_pages+1):
        payload=transport.request("newsapi","GET","https://newsapi.org/v2/everything",
            params={"q":query,"from":start,"to":end,"sortBy":"publishedAt","pageSize":100,"page":page},
            headers={"X-Api-Key":transport.credentials.get("NEWS_API_KEY").get_secret_value()})
        if payload.get("status")!="ok" or not isinstance(payload.get("articles"),list):
            raise RuntimeError("newsapi: rejected request or invalid articles")
        batch=payload["articles"]
        total=int(payload.get("totalResults",0))
        received+=len(batch)
        for article in batch:
            if not article.get("url") or not article.get("publishedAt"):
                continue
            published=pd.Timestamp(article["publishedAt"])
            if published.tzinfo is None:
                raise RuntimeError("newsapi: publication requires timezone")
            doc_id=hashlib.sha256((article["url"]+"|"+published.isoformat()).encode()).hexdigest()
            if doc_id in seen:
                continue
            seen.add(doc_id)
            records.append(dict(document_id=doc_id,title=article.get("title"),description=article.get("description"),
                url=article["url"],source=article.get("source"),published_at=published.tz_convert("UTC").isoformat(),
                available_at=max(published,pd.Timestamp(transport.retrieved_at)).isoformat(),
                ticker_ids=list(tickers),entity_mapping="query_association_unverified",content_truncated=True))
        if received>=total or not batch:
            break
    return _result(transport,"newsapi","everything","news",records,query=query,complete=received>=total,
                   received=received,total_results=total,sentiment_model="not_applied")


class TwinkleClient:
    def __init__(self,transport):
        self.transport=transport
        self.session_id=None
        self.sequence=0

    def _rpc(self,method,params,notification=False):
        self.sequence+=1
        body=dict(jsonrpc="2.0",method=method,params=params)
        if not notification:
            body["id"]=self.sequence
        headers={"Authorization":"Bearer "+self.transport.credentials.get("TWINKLE_API_KEY").get_secret_value(),
                 "Accept":"application/json, text/event-stream","Content-Type":"application/json"}
        if self.session_id:
            headers["mcp-session-id"]=self.session_id
            headers["MCP-Protocol-Version"]="2025-03-26"
        result=self.transport.request("twinkle","POST","https://api.twinkleai.tw/mcp/",json=body,headers=headers)
        self.session_id=self.transport.last_headers.get("mcp-session-id",self.session_id)
        if isinstance(result,dict) and result.get("error"):
            raise RuntimeError("twinkle: MCP request rejected; inspect tool/schema and permissions")
        if not notification and result.get("id")!=body["id"]:
            raise RuntimeError("twinkle: mismatched MCP response id")
        return result.get("result",{})

    def call(self, tool, arguments):
        if not re.fullmatch(r"(?:tw|us|jp|kr|hk|sg|uk)_(?:search|lookup|list|query|get|statute)_[a-z0-9_]+",tool):
            raise ValueError("Only read-only Twinkle data tool_ids are allowed")
        if not self.session_id:
            self._rpc("initialize",dict(protocolVersion="2025-03-26",capabilities={},clientInfo=dict(name="twquant",version="0.2.0")))
            self._rpc("notifications/initialized",{},notification=True)
        result=self._rpc("tools/call",dict(name="execute_tool",arguments=dict(tool_id=tool,arguments=arguments)))
        if result.get("isError"):
            raise RuntimeError("twinkle: data tool returned an error")
        return _result(self.transport,"twinkle",tool,"government",[result],
                       arguments=arguments,mapping="raw_schema_requires_explicit_mapping",complete=False)


def fetch_fubon_prices(transport, tickers, start, end, sdk=None):
    if sdk is None:
        try:
            from fubon_neo.sdk import FubonSDK
        except ImportError:
            raise RuntimeError("Fubon official SDK is not installed; install the official wheel matching this Python/OS") from None
        c=transport.credentials
        login_key="FUBON_API_KEY" if c.status()["FUBON_API_KEY"] else "FUBON_PASSWORD"
        for key in ("FUBON_PERSONAL_ID",login_key,"FUBON_CERT_PATH"):
            c.get(key)
        from .credentials import private_certificate_path
        cert=private_certificate_path(c.get('FUBON_CERT_PATH').get_secret_value())
        sdk=FubonSDK()
        values=[c.get(k).get_secret_value() for k in ("FUBON_PERSONAL_ID",login_key,"FUBON_CERT_PATH")]
        values[2]=str(cert)
        if c.status()["FUBON_CERT_PASSWORD"]:
            values.append(c.get("FUBON_CERT_PASSWORD").get_secret_value())
        try:
            login=(sdk.apikey_login if login_key=="FUBON_API_KEY" else sdk.login)(*values)
            if not getattr(login,"is_success",False):
                raise RuntimeError("login rejected")
            sdk.init_realtime()
        except Exception:
            raise RuntimeError("Fubon authentication/market-data initialization failed; sensitive response omitted") from None
    def get(ticker,first,last,adjusted):
        params={"symbol":ticker,"from":first,"to":last,"timeframe":"D","adjusted":str(adjusted).lower(),
                "sort":"asc","fields":"open,high,low,close,volume"}
        try:
            payload=sdk.marketdata.rest_client.stock.historical.candles(**params)
        except Exception as exc:
            if getattr(exc,'status_code',None)==404:
                raise SourceHTTPError('fubon',404) from None
            raise RuntimeError("Fubon market-data read failed; sensitive SDK response omitted") from None
        transport.retrieved_at=now_utc()
        transport.archive("fubon","historical/candles",params,payload)
        return payload
    return _paired_prices("fubon",tickers,start,end,get)


def fetch_public(transport, provider, dataset):
    if (provider,dataset) not in PUBLIC_ROUTES:
        raise ValueError("Unsupported official public dataset")
    payload=transport.request(provider,"GET",PUBLIC_ROUTES[(provider,dataset)])
    if provider=='twse' and dataset in {'announcements','calendar'}:
        from .public_sources import normalize_announcements,normalize_calendar
        return (normalize_announcements if dataset=='announcements' else normalize_calendar)(payload,transport.retrieved_at)
    return _result(transport,provider,dataset,"official_snapshot",payload,
                   availability_basis="observed_retrieval_only",historical=False,complete=True)


def _event(result,feature,value,period,published,available,ticker="*",version=0,basis="observed_retrieval_only"):
    feature=re.sub("[^A-Za-z0-9_]","_",feature)
    if len(feature)>63:
        feature=feature[:50]+"_"+hashlib.sha256(feature.encode()).hexdigest()[:10]
    return dict(ticker=ticker,feature=feature,value=float(value),period_end=str(period),
        published_at=pd.Timestamp(published).tz_convert("UTC"),available_at=pd.Timestamp(available).tz_convert("UTC"),
        version=version,source=result.provider,availability_basis=basis,data_kind="historical",
        retrieved_at=result.retrieved_at)


def events_from_result(result):
    """Convert only defined numeric schemas, never invent sentiment or announcement times."""
    rows=[]
    if result.provider=='tdcc' and result.dataset=='distribution':
        for record in result.records:
            if not 1<=record['band']<=15:continue
            for metric in ('ratio','holders'):
                rows.append(_event(result,f'tdcc_band_{record["band"]}_{metric}',record[metric],record['date'],
                    result.retrieved_at,result.retrieved_at,ticker=record['ticker']))
    elif result.provider=="fred":
        records=pd.DataFrame(result.records)
        if not records.empty:
            required={"date","realtime_start","value"}
            if not required<=set(records):
                raise ValueError("FRED observations lack vintage metadata")
            records["value"]=pd.to_numeric(records.value,errors="coerce")
            records=records[np.isfinite(records.value)].copy()
            # Some daily observations are future-dated relative to their vendor vintage.
            # Preserve the raw vintage, but withhold the feature until BOTH dates end.
            records['effective_day']=[max(str(r.realtime_start),str(r.date)) for r in records.itertuples()]
            records=records.sort_values(['effective_day','realtime_start','date'])
            known={}
            for version,(vintage,updates) in enumerate(records.groupby('effective_day',sort=True)):
                for record in updates.to_dict("records"):
                    known[record["date"]]=record["value"]
                latest=max(known)
                # FRED supplies a vintage DAY, not a release time. Use the NEXT
                # New York midnight to avoid pretending an 08:30 release was known at00:00.
                available=(pd.Timestamp(vintage)+pd.Timedelta(days=1)).tz_localize("America/New_York").tz_convert("UTC")
                rows.append(_event(result,"fred_"+result.dataset,known[latest],latest,available,available,
                    version=version,basis="vintage_day_conservative"))
    elif result.provider=="fmp" and result.dataset in {"prices","adjusted_prices"}:
        data=pd.DataFrame(result.records)
        if not data.empty:
            if result.dataset=='adjusted_prices' and 'adjClose' in data:
                data['close']=data['adjClose']
            if not {"date","close"}<=set(data):
                raise ValueError("FMP price schema mismatch")
            data=data.sort_values("date")
            if data.date.duplicated().any():
                raise ValueError("FMP duplicate market dates")
            data["close"]=pd.to_numeric(data.close,errors="raise")
            if not np.isfinite(data.close).all() or (data.close<=0).any():
                raise ValueError("FMP close must be finite and positive")
            returns=data.close.pct_change(fill_method=None)
            for index,record in data.iterrows():
                available=(pd.Timestamp(record.date).tz_localize("America/New_York")+pd.Timedelta(hours=17)).tz_convert("UTC")
                prefix="fmp_"+result.metadata["symbol"].lower()
                rows.append(_event(result,prefix+"_close",record.close,record.date,available,available,
                                   basis="reconstructed_us_close_plus_one_hour"))
                if pd.notna(returns.loc[index]):
                    rows.append(_event(result,prefix+"_return1",returns.loc[index],record.date,available,available,
                                       basis="reconstructed_us_close_plus_one_hour"))
    elif result.provider=='finmind' or result.provider=='fmp' and result.kind=='financial' and result.dataset!='estimates':
        numeric={'finmind':{'revenue','buy','sell','value','PER','PBR','dividend_yield'},
                 'fmp':{'revenue','eps','netIncome','grossProfit','operatingIncome','totalAssets','totalLiabilities',
                        'totalStockholdersEquity','operatingCashFlow','capitalExpenditure','freeCashFlow'}}
        candidates=[]
        short_names={'TaiwanStockInstitutionalInvestorsBuySell':'institutional',
            'TaiwanStockFinancialStatements':'income','TaiwanStockBalanceSheet':'balance',
            'TaiwanStockCashFlowsStatement':'cashflow','TaiwanStockMonthRevenue':'revenue'}
        dataset_name=short_names.get(result.dataset,result.dataset)
        for record in result.records:
            ticker=str(record.get('stock_id',result.metadata.get('symbol','*')))
            period=str(record.get('date',result.retrieved_at[:10]))[:10]
            for key in numeric[result.provider]:
                value=record.get(key)
                if isinstance(value,bool):
                    continue
                try:
                    value=float(value)
                except (ValueError,TypeError):
                    continue
                if not np.isfinite(value):
                    continue
                metric=str(record.get('type',record.get('name',key))) if key=='value' else key
                if record.get('name') and key in {'buy','sell'}:
                    metric=str(record['name'])+'_'+key
                feature=f'{result.provider}_{dataset_name}_{metric}'
                candidates.append(_event(result,feature,value,period,result.retrieved_at,result.retrieved_at,ticker=ticker))
            if result.provider=='finmind' and record.get('name') and 'buy' in record and 'sell' in record:
                try:
                    net=float(record['buy'])-float(record['sell'])
                except (TypeError,ValueError):
                    continue
                if np.isfinite(net):
                    candidates.append(_event(result,f'{result.provider}_{dataset_name}_{record["name"]}_net',net,
                        period,result.retrieved_at,result.retrieved_at,ticker=ticker))
        # A freshly downloaded historical financial snapshot is known NOW.
        # Never relabel a current/restated value as if seen on the original filing date.
        if candidates:
            current=pd.DataFrame(candidates).sort_values('period_end').drop_duplicates(['ticker','feature'],keep='last')
            rows=current.to_dict('records')
    else:
        raise ValueError(f"No automatic numeric mapping for {result.provider}/{result.dataset}; raw snapshot retained")
    return pd.DataFrame(rows,columns=["ticker","feature","value","period_end","published_at","available_at",
        "version","source","availability_basis","data_kind","retrieved_at"])


def holdings_from_result(result):
    if result.kind!="membership":
        raise ValueError("Membership result required")
    rows=[]
    for daily in result.records:
        if not {"date","components"}<=set(daily):
            raise ValueError("ETF holdings schema mismatch")
        total=0.
        for component in daily["components"]:
            weight=float(component["weight"])/100
            if not np.isfinite(weight) or not 0<=weight<=1:
                raise ValueError("Invalid ETF component weight")
            total+=weight
            rows.append(dict(index_id=result.metadata.get("symbol","0050"),ticker=str(component["symbol"]),
                name=component.get("name"),weight=weight,valid_from=daily["date"],available_at=result.retrieved_at,
                announced_at=None,historical_announcement_verified=False,source=result.provider))
        if total>1.02:
            raise ValueError("ETF component weights exceed100%; verify units")
    return pd.DataFrame(rows)
