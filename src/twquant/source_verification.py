"""Repeatable bounded live checks. Reports contain counts and errors, never credentials/data rows."""
from datetime import datetime,timezone
from pathlib import Path
import importlib.util
import sys
import time
import uuid

import pandas as pd

from .credentials import redact
from .data import PriceStore
from .source_store import SourceStore
from .source_workflows import source_status
from .sources import (SourceTransport,SourceHTTPError,fetch_fred,fetch_fmp,fetch_finmind,fetch_fugle,
    fetch_fugle_prices,fetch_fubon_prices,fetch_news,fetch_public,TwinkleClient,events_from_result)
from .workflows import write_json


def _probes(provider,start,end,full):
    jobs=[]
    if provider=='finmind':
        datasets=['TaiwanStockPrice','TaiwanStockPriceAdj','TaiwanStockInstitutionalInvestorsBuySell','TaiwanStockFinancialStatements']
        if full:datasets+=['TaiwanStockBalanceSheet','TaiwanStockCashFlowsStatement','TaiwanStockMonthRevenue']
        for dataset in datasets:
            first=str(pd.Timestamp(end).date().replace(year=pd.Timestamp(end).year-2)) if 'Statement' in dataset or 'Revenue' in dataset or 'Balance' in dataset else start
            jobs.append((dataset,lambda t,d=dataset,a=first:fetch_finmind(t,d,'2330',a,end)))
    elif provider=='fred':
        series=['GDP','CPIAUCSL','PCEPI','UNRATE','PAYEMS','FEDFUNDS','DGS10','DGS2','M2SL','VIXCLS','DEXTAUS'] if full else ['GDP','DGS10','VIXCLS']
        for series_id in series:
            jobs.append((series_id,lambda t,s=series_id:fetch_fred(t,s,end[:4]+'-01-01',end)))
    elif provider=='fmp':
        for symbol in (['SPY','QQQ','SOXX','TSM','NVDA'] if full else ['SPY','QQQ','TSM']):
            jobs.append((symbol,lambda t,s=symbol:fetch_fmp(t,'adjusted_prices',s,start,end)))
        for dataset in (['income','balance','cashflow','estimates'] if full else ['income','estimates']):
            jobs.append((dataset,lambda t,d=dataset:fetch_fmp(t,d,'TSM',start,end,period='annual' if d=='estimates' else 'quarter')))
    elif provider=='fugle':
        jobs.append(('paired-prices',lambda t:fetch_fugle_prices(t,['2330','0050'],start,end)))
        for dataset in (['holdings','institutional','tdcc','quote'] if full else ['holdings']):
            jobs.append((dataset,lambda t,d=dataset:fetch_fugle(t,d,'0050' if d=='holdings' else '2330',start,end)))
    elif provider=='newsapi':
        jobs.append(('recent',lambda t:fetch_news(t,'TSMC OR semiconductor',start,end,['2330'],max_pages=1)))
    elif provider=='twinkle':
        jobs.append(('industry-discovery',lambda t:TwinkleClient(t).call('tw_search_datasets',{'query':'半導體','limit':2})))
    elif provider in {'twse','taifex'}:
        for dataset in (['prices','companies','revenue'] if provider=='twse' else ['futures']):
            jobs.append((dataset,lambda t,d=dataset:fetch_public(t,provider,d)))
    elif provider=='fubon':
        jobs.append(('paired-prices',lambda t:fetch_fubon_prices(t,['0050'],start,end)))
    if full and provider in {'fugle','fmp','newsapi'}:
        fn={'fugle':lambda t:fetch_fugle_prices(t,['0050'],'2014-01-02','2014-01-10'),
            'fmp':lambda t:fetch_fmp(t,'adjusted_prices','SPY','2014-01-02','2014-01-10'),
            'newsapi':lambda t:fetch_news(t,'TSMC','2020-01-02','2020-01-03',['2330'],max_pages=1)}[provider]
        jobs.append(('historical-access',fn))
    return jobs


def _fubon_prerequisites(credentials,missing):
    present=credentials.status()
    certificate=Path(credentials.get('FUBON_CERT_PATH').get_secret_value()).expanduser() if present['FUBON_CERT_PATH'] else None
    exists=bool(certificate and certificate.is_file() and not certificate.is_symlink())
    owner_only=bool(exists and not certificate.stat().st_mode&0o077)
    sdk=importlib.util.find_spec('fubon_neo') is not None
    supported=(3,8)<=sys.version_info[:2]<=(3,13)
    blockers=[]
    if missing:blockers.append('missing_credentials')
    if not sdk:blockers.append('sdk_not_installed')
    if not supported:blockers.append('unsupported_python_for_official_sdk')
    if not exists:blockers.append('certificate_missing_or_symlink')
    elif not owner_only:blockers.append('certificate_permissions')
    return dict(blockers=blockers,sdk_installed=sdk,python_supported=supported,
                certificate_exists=exists,certificate_owner_only=owner_only,login_attempted=False)


def verify_sources(root,credentials,full=False,providers=None):
    root=Path(root)
    statuses={row['provider']:row for row in source_status(credentials)}
    selected=list(dict.fromkeys(providers or statuses))
    if not selected or any(provider not in statuses for provider in selected):
        raise ValueError('Choose registered verification providers')
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    folder=root/'sources/verifications'/run_id
    folder.mkdir(parents=True,exist_ok=False)
    t=SourceTransport(credentials,raw_dir=folder/'raw')
    checks=[]
    for provider in selected:
        missing=statuses[provider]['missing_fields']
        prerequisites=_fubon_prerequisites(credentials,missing) if provider=='fubon' else None
        if missing or prerequisites and prerequisites['blockers']:
            row=dict(provider=provider,check='prerequisites',status='blocked',missing_fields=missing)
            if prerequisites:row.update(prerequisites)
            checks.append(row)
            continue
        for name,fetch in _probes(provider,
            (pd.Timestamp.now(tz='Asia/Taipei').normalize()-pd.Timedelta(days=11)).date().isoformat(),
            (pd.Timestamp.now(tz='Asia/Taipei').normalize()-pd.Timedelta(days=1)).date().isoformat(),full):
            started=time.monotonic()
            row=dict(provider=provider,check=name,status='failed')
            try:
                data=fetch(t)
                if isinstance(data,pd.DataFrame):
                    sid=PriceStore(folder/provider/name/'data').write_prices(data)
                    row.update(records=len(data),model_prices=True,snapshot=sid,
                        date_min=str(data.date.min().date()),date_max=str(data.date.max().date()))
                else:
                    row.update(records=len(data.records),kind=data.kind,complete=data.metadata.get('complete',False))
                    row['snapshot']=SourceStore(folder/provider/name/'sources').write(data,secrets=credentials.secret_values())
                    mapped=SourceStore._has_mapping(data.provider,data.dataset,data.kind)
                    row['numeric_mapping']=mapped
                    if mapped:row['events']=len(events_from_result(data))
                    dates=[str(r['date']) for r in data.records if r.get('date')]
                    if dates:row.update(date_min=min(dates),date_max=max(dates))
                    if data.metadata.get('record_limit'):row['record_limit']=data.metadata['record_limit']
                row['status']='passed' if row['records'] else 'empty'
                if provider=='fubon':row['login_attempted']=True
            except Exception as exc:
                row.update(error_type=type(exc).__name__,error=redact(str(exc),credentials.secret_values()))
                if isinstance(exc,SourceHTTPError):
                    row['http_status']=exc.status_code
                    row['status']='entitlement_blocked' if exc.status_code in {402,403,426} else 'failed'
                if provider=='fubon':row['login_attempted']=True
            row['seconds']=round(time.monotonic()-started,2)
            checks.append(row)
    summaries=[]
    for provider in selected:
        own=[row for row in checks if row['provider']==provider]
        passed=sum(row['status']=='passed' for row in own)
        summaries.append(dict(provider=provider,passed=passed,total=len(own),
            status='passed' if passed==len(own) else 'partial' if passed else 'blocked' if all(r['status']=='blocked' for r in own) else 'failed'))
    result=redact(dict(run_id=run_id,checked_at=datetime.now(timezone.utc).isoformat(),
        passed=all(row['status']=='passed' for row in checks),full=full,providers=summaries,checks=checks,
        scope='Bounded read-only transport, schema and snapshot checks; not proof of complete historical coverage or profitable forecasts.',
        credentials_exposed=False,primary_history_replaced=False),credentials.secret_values())
    write_json(folder/'report.json',result)
    write_json(root/'sources/verification.json',result)
    return result
