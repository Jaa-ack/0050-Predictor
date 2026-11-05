"""Source catalog, explicit acquisitions and model-input exports."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .credentials import redact
from .data import PriceStore
from .ingestion import ingest_finmind
from .source_store import SourceStore
from .sources import (SourceTransport,fetch_fred,fetch_fmp,fetch_finmind,fetch_fugle,fetch_fugle_prices,
                      fetch_news,fetch_fubon_prices,fetch_public,TwinkleClient)
from .workflows import write_json

REQUIRED={"finmind":["FINMIND_TOKEN"],"fred":["FRED_API_KEY"],"fmp":["FMP_API_KEY"],
          "newsapi":["NEWS_API_KEY"],"twinkle":["TWINKLE_API_KEY"],"fugle":["FUGLE_API_KEY"],
          "fubon":["FUBON_PERSONAL_ID","FUBON_CERT_PATH"],"twse":[],"taifex":[]}


def source_status(credentials):
    present=credentials.status()
    statuses=[]
    for provider,required in REQUIRED.items():
        missing=[k for k in required if not present[k]]
        if provider=='fubon' and not (present['FUBON_API_KEY'] or present['FUBON_PASSWORD']):
            missing.append('FUBON_API_KEY or FUBON_PASSWORD')
        row=dict(provider=provider,status='missing_credentials' if missing else 'configured_unverified' if required else 'public_unverified',
                 missing_fields=missing,entitlement='not_verified',credential_values_exposed=False)
        if provider=='fubon':
            row['sdk_installed']=importlib.util.find_spec('fubon_neo') is not None
            row['scope']='market_data_only'
        statuses.append(row)
    return statuses


def _execute_job(t,job):
    provider,dataset=job['provider'],job['dataset']
    start=job.get('start','2014-01-01')
    end=job.get('end','today')
    if end=='today':
        end=pd.Timestamp.now(tz='Asia/Taipei').date().isoformat()
    symbol=job.get('symbol','2330')
    if provider=='fred':
        return fetch_fred(t,dataset,start,end)
    if provider=='fmp':
        return fetch_fmp(t,dataset,symbol,start,end)
    if provider=='newsapi':
        return fetch_news(t,job['query'],start,end,job.get('tickers',[]),job.get('max_pages',10))
    if provider=='twinkle':
        return TwinkleClient(t).call(dataset,job.get('arguments',{}))
    if provider in {'twse','taifex'}:
        return fetch_public(t,provider,dataset)
    if provider in {'fugle','fubon','finmind'} and dataset=='prices':
        tickers=job.get('tickers',[symbol])
        if provider=='fugle':
            return fetch_fugle_prices(t,tickers,start,end)
        if provider=='fubon':
            return fetch_fubon_prices(t,tickers,start,end)
        return ingest_finmind(tickers,start,end,t.credentials.get('FINMIND_TOKEN').get_secret_value(),raw_dir=t.raw_dir)
    if provider=='finmind':
        return fetch_finmind(t,dataset,symbol,start,end)
    if provider=='fugle':
        return fetch_fugle(t,dataset,symbol,start,end)
    raise ValueError('Unsupported source job; choose a registered read-only provider/dataset')


def sync_sources(root,plan_path,credentials):
    root=Path(root)
    plan=json.loads(Path(plan_path).read_text())
    jobs=plan.get('jobs')
    if not isinstance(jobs,list) or len(jobs)>200:
        raise ValueError('Source plan requires at most200 explicit jobs')
    ids=[job.get('id') for job in jobs]
    if any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
        raise ValueError('Source jobs require unique nonempty id strings')
    statuses={s['provider']:s for s in source_status(credentials)}
    transport=SourceTransport(credentials,raw_dir=root/'sources'/'raw')
    store=SourceStore(root/'sources')
    report=dict(jobs=[],price_provider=plan.get('price_provider','fugle'),automatic_trading=False)
    for job in jobs:
        row=dict(id=job['id'],provider=job['provider'],dataset=job['dataset'])
        if not job.get('enabled',False):
            row['status']='disabled'
        elif job['provider'] not in statuses:
            row.update(status='error',error='Unknown provider')
        elif statuses[job['provider']]['status']=='missing_credentials':
            row.update(status='missing_credentials',missing_fields=statuses[job['provider']]['missing_fields'])
        elif job['provider']=='fubon' and not statuses['fubon']['sdk_installed']:
            row.update(status='missing_sdk',error='Install the official Fubon SDK wheel matching this Python/OS')
        else:
            try:
                data=_execute_job(transport,job)
                if isinstance(data,pd.DataFrame):
                    sid=PriceStore(root/'source_prices'/job['provider']).write_prices(data,dict(source_job=job['id']))
                    row.update(records=len(data),snapshot_id=sid,kind='adjusted_daily_prices')
                    if job['provider']==report['price_provider']:
                        row['primary_snapshot_id']=PriceStore(root/'data').write_prices(data,dict(source_job=job['id'],provider=job['provider']))
                else:
                    sid=store.write(data,secrets=credentials.secret_values())
                    row.update(records=len(data.records),snapshot_id=sid,kind=data.kind,
                               complete=data.metadata.get('complete',False))
                row['status']='ok'
            except (ValueError,RuntimeError,OSError,KeyError,TypeError) as exc:
                row.update(status='error',error=redact(str(exc),credentials.secret_values()))
        report['jobs'].append(row)
    report['ok']=sum(r['status']=='ok' for r in report['jobs'])
    report['blocked']=sum(r['status'] in {'missing_credentials','missing_sdk','error'} for r in report['jobs'])
    write_json(root/'sources'/'sync_report.json',redact(report,credentials.secret_values()))
    return report


def reconcile_prices(left,right,relative_tolerance=.001):
    for frame in (left,right):
        if not {'ticker','date','close'}<=set(frame) or frame.duplicated(['ticker','date']).any():
            raise ValueError('Reconciliation requires unique ticker/date raw closes')
    merged=left[['ticker','date','close']].merge(right[['ticker','date','close']],on=['ticker','date'],suffixes=('_left','_right'))
    if merged.empty:
        return dict(passed=False,overlap=0,mismatches=0,reason='No common market observations')
    error=(merged.close_left-merged.close_right).abs()/merged.close_left
    if not np.isfinite(error).all() or (merged.close_left<=0).any() or (merged.close_right<=0).any():
        raise ValueError('Reconciliation prices must be finite and positive')
    bad=merged.loc[error>relative_tolerance,['ticker','date']]
    return dict(passed=bad.empty,overlap=len(merged),mismatches=len(bad),max_relative_error=float(error.max()),
                samples=[dict(ticker=r.ticker,date=str(r.date)) for r in bad.head(20).itertuples()],
                policy='Compare raw closes; never silently merge or select vendor by favorable backtest')
