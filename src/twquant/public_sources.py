"""Documented free feeds with explicit coverage, provenance and conservative receipt times."""
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

import numpy as np
import pandas as pd
import requests

from .sources import SourceResult,SourceHTTPError,now_utc,identifier


def _date(value):
    digits=re.sub(r'[/.-]','',str(value).strip())
    if not digits.isdigit() or len(digits) not in {7,8}:
        raise ValueError('Official date must be a Gregorian or ROC daily date')
    year=int(digits[:-4])+(1911 if len(digits)==7 else 0)
    return pd.Timestamp(year=year,month=int(digits[-4:-2]),day=int(digits[-2:])).date().isoformat()


def _clean(row):
    result={str(key).lstrip('\ufeff').strip():value for key,value in row.items()}
    if len(result)!=len(row):raise ValueError('Ambiguous public-source column names')
    return result


def normalize_announcements(raw,retrieved_at):
    records=[]
    received=pd.Timestamp(retrieved_at)
    for raw_row in raw:
        row=_clean(raw_row)
        required={'公司代號','發言日期','發言時間','主旨','說明'}
        if not required<=set(row):raise ValueError('TWSE disclosure schema mismatch')
        clock=str(row['發言時間']).strip().replace(':','').zfill(6)
        if not re.fullmatch(r'\d{6}',clock):raise ValueError('TWSE speech time malformed')
        day=_date(row['發言日期'])
        published=pd.Timestamp(f'{day} {clock[:2]}:{clock[2:4]}:{clock[4:]}',tz='Asia/Taipei').tz_convert('UTC')
        ticker=identifier(str(row['公司代號']).strip())
        identity='|'.join([ticker,published.isoformat(),str(row['主旨'])])
        records.append(dict(document_id=hashlib.sha256(identity.encode()).hexdigest(),ticker_ids=[ticker],
            title=str(row['主旨']),description=str(row['說明']),source='twse_material_disclosure',
            published_at=published.isoformat(),available_at=max(received,published).isoformat(),
            fact_date=row.get('事實發生日'),clause=row.get('符合條款'),url=None,
            entity_mapping='official_company_code',availability_basis='observed_retrieval_only'))
    return SourceResult('twse','announcements','news',records,retrieved_at,
        dict(complete=True,coverage='current_official_snapshot_only',license='Open Government Data License Taiwan v1',
             limitation='Speech date/time is distinct from fact date. Historical revisions and sentiment are not inferred.'))


def normalize_calendar(raw,retrieved_at):
    records=[]
    for row in raw:
        if not {'Name','Date','Description'}<=set(row):raise ValueError('TWSE calendar schema mismatch')
        name=str(row['Name']).strip()
        description=re.sub('<[^>]*>','',str(row['Description']))
        if any(token in name for token in ['最後交易日','開始交易日']):hint=1
        elif '無交易' in name or any(token in description for token in ['放假','補假','休市','停止交易']):hint=0
        else:hint=None
        records.append(dict(date=_date(row['Date']),name=name,description=description,is_open_hint=hint))
    return SourceResult('twse','calendar','calendar',records,retrieved_at,
        dict(complete=True,coverage='annual_event_list_not_complete_daily_or_emergency_calendar',
             verified=False,license='Open Government Data License Taiwan v1'))


def calendar_candidate(result,year):
    events=[row for row in result.records if row['date'].startswith(str(year)+'-')]
    if not events:raise ValueError('Requested calendar year is not in this official snapshot')
    if any(row['is_open_hint'] is None for row in events):raise ValueError('Calendar contains unclassified/unknown events')
    overrides={}
    for row in events:
        if row['date'] in overrides and overrides[row['date']]!=row['is_open_hint']:
            raise ValueError('Conflicting official calendar events')
        overrides[row['date']]=row['is_open_hint']
    days=pd.date_range(f'{year}-01-01',f'{year}-12-31')
    return pd.DataFrame([dict(date=day.date().isoformat(),is_open=overrides.get(day.date().isoformat(),int(day.day_of_week<5)),
        source='TWSE annual schedule candidate; emergency closures must be verified',verified=False,
        available_at=result.retrieved_at) for day in days])


def normalize_tdcc(raw,retrieved_at,tickers=()):
    selected={identifier(str(ticker)) for ticker in tickers}
    records=[]
    required={'資料日期','證券代號','持股分級','人數','股數','占集保庫存數比例%'}
    for raw_row in raw:
        row=_clean(raw_row)
        if not required<=set(row):raise ValueError('TDCC public ownership schema mismatch')
        ticker=str(row['證券代號']).strip()
        if selected and ticker not in selected:continue
        band=int(str(row['持股分級']).strip())
        ratio=float(str(row['占集保庫存數比例%']).replace(',',''))/100
        holders=int(str(row['人數']).replace(',',''))
        shares=int(str(row['股數']).replace(',',''))
        if not 1<=band<=17 or not np.isfinite(ratio) or not 0<=ratio<=1 or holders<0:
            raise ValueError('TDCC band/count/percentage is invalid')
        if shares<0 and band!=16:raise ValueError('TDCC ordinary ownership band has negative shares')
        records.append(dict(ticker=ticker,date=_date(row['資料日期']),band=band,ratio=ratio,
                            holders=holders,shares=shares))
    if selected-set(row['ticker'] for row in records):raise ValueError('TDCC response misses requested instruments')
    identity=[(row['ticker'],row['date'],row['band']) for row in records]
    if len(identity)!=len(set(identity)):raise ValueError('TDCC duplicate ownership bands')
    return SourceResult('tdcc','distribution','ownership',records,retrieved_at,
        dict(complete=True,coverage='current_official_snapshot_only',availability_basis='observed_retrieval_only',
             license='Open Government Data License Taiwan v1',
             limitation='No historical publication timestamp inferred; grade16 correction and grade17 total are not model inputs.'))


def fetch_tdcc(t,tickers=()):
    raw=t.request('tdcc','GET','https://openapi.tdcc.com.tw/v1/opendata/1-5')
    if not isinstance(raw,list):raise ValueError('TDCC response must be a record array')
    return normalize_tdcc(raw,t.retrieved_at,tickers)


def _download_once(t,url,max_bytes):
    """Fixed public CDN only; no auth, redirects, HTTP downgrade or arbitrary archive URLs."""
    if not re.fullmatch(r'https://data\.gdeltproject\.org/gdeltv2/(?:lastupdate\.txt|\d{14}\.gkg\.csv\.zip)',url):
        raise ValueError('GDELT download requires its fixed HTTPS manifest/archive path')
    response=None
    try:
        response=t.session.request('GET',url,timeout=(10,40),allow_redirects=False,stream=True,
            headers={'Accept-Encoding':'identity'})
        if response.status_code!=200:raise SourceHTTPError('gdelt',response.status_code)
        body=bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body)>max_bytes:raise ValueError('GDELT download exceeds the bounded size')
        t.retrieved_at=now_utc()
        if t.raw_dir is not None:
            folder=Path(t.raw_dir);folder.mkdir(parents=True,exist_ok=True)
            name='gdelt_'+uuid.uuid4().hex
            (folder/(name+'.bin')).write_bytes(body)
            (folder/(name+'.sha256')).write_text(hashlib.sha256(body).hexdigest()+'\n')
        return bytes(body)
    finally:
        if response is not None:response.close()


def _download(t,url,max_bytes):
    for attempt in range(2):
        try:
            return _download_once(t,url,max_bytes)
        except requests.RequestException:
            if attempt:
                raise RuntimeError('GDELT public download failed after bounded retry; no synthetic news supplied') from None
            t.sleeper(1.)


def parse_gkg_archive(payload,digest,batch,retrieved_at,terms=(),max_records=100):
    if hashlib.md5(payload,usedforsecurity=False).hexdigest()!=digest:
        raise ValueError('GDELT archive checksum mismatch')
    if not re.fullmatch(r'\d{14}',batch) or not 1<=max_records<=1000:
        raise ValueError('Invalid GDELT batch or record bound')
    if not isinstance(terms,(list,tuple)) or len(terms)>20 or any(not isinstance(term,str) or not 1<=len(term)<=100 for term in terms):
        raise ValueError('GDELT terms require a bounded list of nonempty strings')
    records=[];seen=set();scanned=0;matched=0
    batch_ts=pd.to_datetime(batch,format='%Y%m%d%H%M%S',utc=True)
    available=max(pd.Timestamp(retrieved_at),batch_ts).isoformat()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members=archive.infolist()
            if len(members)!=1 or members[0].filename!=batch+'.gkg.csv' or members[0].file_size>32_000_000:
                raise ValueError('Unexpected or oversized GDELT archive member')
            with io.TextIOWrapper(archive.open(members[0]),encoding='utf-8') as handle:
                for row in csv.reader(handle,delimiter='\t'):
                    scanned+=1
                    if scanned>50000 or len(row)!=27:raise ValueError('GDELT GKG schema/row bound exceeded')
                    searchable=' '.join([row[4],row[7],row[8],row[13],row[14]]).casefold()
                    if terms and not any(term.casefold() in searchable for term in terms):continue
                    if not re.match(r'^https?://',row[4]):continue
                    if row[4] in seen:continue
                    seen.add(row[4]);matched+=1
                    if len(records)>=max_records:continue
                    records.append(dict(document_id=hashlib.sha256(row[4].encode()).hexdigest(),url=row[4],
                        title=None,published_at=None,available_at=available,batch_ts=batch_ts.isoformat(),
                        source='gdelt_gkg',organizations=row[13],themes=row[7],ticker_ids=[],
                        entity_mapping='keyword_association_unverified',availability_basis='observed_retrieval_only'))
    except (zipfile.BadZipFile,csv.Error):
        raise ValueError('Malformed GDELT data archive') from None
    return SourceResult('gdelt','gkg','news',records,retrieved_at,
        dict(complete=matched<=max_records,coverage='one_latest_15_minute_batch_only',scanned=scanned,matched=matched,
             batch=batch,terms=list(terms),poll_interval_minutes=15,
             limitation='Metadata and URLs only. Batch time is not article publication time; no full text, title or sentiment invented.'))


def fetch_gdelt(t,terms=(),max_records=100):
    manifest=_download(t,'https://data.gdeltproject.org/gdeltv2/lastupdate.txt',4096).decode('ascii')
    matches=[]
    for line in manifest.splitlines():
        parts=line.split()
        if len(parts)!=3:raise ValueError('GDELT manifest schema mismatch')
        size,digest,url=parts
        match=re.fullmatch(r'https?://data\.gdeltproject\.org/gdeltv2/(\d{14})\.gkg\.csv\.zip',url)
        if match:matches.append((int(size),digest,match[1]))
    if len(matches)!=1:raise ValueError('GDELT manifest must identify one GKG archive')
    size,digest,batch=matches[0]
    if not 0<size<=8_000_000 or not re.fullmatch(r'[a-f0-9]{32}',digest):
        raise ValueError('GDELT archive manifest bound/digest invalid')
    payload=_download(t,f'https://data.gdeltproject.org/gdeltv2/{batch}.gkg.csv.zip',8_000_000)
    if len(payload)!=size:raise ValueError('GDELT archive size mismatch')
    return parse_gkg_archive(payload,digest,batch,t.retrieved_at,terms,max_records)
