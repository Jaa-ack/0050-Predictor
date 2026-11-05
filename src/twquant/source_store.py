"""Immutable source snapshots and deterministic exports to the research event schema."""
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import re
import uuid

import pandas as pd

from .credentials import redact
from .sources import SourceResult,events_from_result,holdings_from_result


class SourceStore:
    def __init__(self,root):
        self.root=Path(root)

    def write(self,result,secrets=()):
        sid=uuid.uuid4().hex
        folder=self.root/'snapshots'/sid
        folder.mkdir(parents=True,exist_ok=False)
        document=redact(asdict(result),secrets)
        body=json.dumps(document,ensure_ascii=False,allow_nan=False,default=str).encode()
        with (folder/'result.json').open('xb') as handle:
            handle.write(body)
        manifest=dict(snapshot_id=sid,provider=result.provider,dataset=result.dataset,kind=result.kind,
                      retrieved_at=result.retrieved_at,records=len(result.records),sha256=hashlib.sha256(body).hexdigest(),
                      metadata=document['metadata'])
        with (folder/'manifest.json').open('x',encoding='utf-8') as handle:
            json.dump(manifest,handle,ensure_ascii=False,indent=2)
        # Only known mappings are exported; unrecognized schemas stay in immutable raw snapshots.
        if self._has_mapping(result.provider,result.dataset,result.kind):
            events_from_result(SourceResult(**document)).to_parquet(folder/'events.parquet',index=False)
        if result.kind=='membership':
            holdings_from_result(result).to_csv(folder/'membership.csv',index=False)
        return sid

    def read(self,snapshot_id):
        if not re.fullmatch(r'[a-f0-9]{32}',snapshot_id):
            raise ValueError('Invalid source snapshot identifier')
        folder=self.root/'snapshots'/snapshot_id
        manifest=json.loads((folder/'manifest.json').read_text())
        body=(folder/'result.json').read_bytes()
        if hashlib.sha256(body).hexdigest()!=manifest['sha256']:
            raise ValueError('Source snapshot checksum mismatch')
        return SourceResult(**json.loads(body))

    def manifests(self):
        return sorted([json.loads(p.read_text()) for p in (self.root/'snapshots').glob('*/manifest.json')],
                      key=lambda m:(m['retrieved_at'],m['snapshot_id']))

    @staticmethod
    def _has_mapping(provider,dataset,kind):
        return provider in {'fred','finmind'} or provider=='tdcc' and dataset=='distribution' or provider=='fmp' and (dataset in {'prices','adjusted_prices'} or kind=='financial' and dataset!='estimates')

    def export_documents(self,path):
        rows=[]
        last_content={}
        for manifest in self.manifests():
            if manifest['kind']!='news':continue
            result=self.read(manifest['snapshot_id'])
            for record in result.records:
                if not {'document_id','available_at'}<=set(record):continue
                content={key:value for key,value in record.items() if key not in {'available_at','retrieved_at'}}
                content_hash=hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                identity=(result.provider,record['document_id'])
                if last_content.get(identity)==content_hash:continue
                last_content[identity]=content_hash
                rows.append({**record,'provider':result.provider,'source_snapshot_id':manifest['snapshot_id'],
                             'content_sha256':content_hash})
        rows.sort(key=lambda row:row['available_at'])
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',encoding='utf-8') as handle:
            for row in rows:handle.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
        return rows

    def export_calendar(self,path,year):
        from .public_sources import calendar_candidate
        matches=[row for row in self.manifests() if row['provider']=='twse' and row['dataset']=='calendar']
        if not matches:raise ValueError('Sync the official TWSE calendar snapshot first')
        frame=calendar_candidate(self.read(matches[-1]['snapshot_id']),year)
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        frame.to_csv(path,index=False)
        return frame

    def export_events(self,path):
        frames=[]
        for manifest in self.manifests():
            if self._has_mapping(manifest['provider'],manifest['dataset'],manifest['kind']):
                result=self.read(manifest['snapshot_id'])
                e=events_from_result(result)
                e['source_snapshot_id']=manifest['snapshot_id']
                frames.append(e)
        if not frames:
            raise ValueError('No normalized numeric events; sync FRED or FMP prices first')
        combined=pd.concat(frames,ignore_index=True)
        identity=['ticker','feature','period_end','published_at','available_at','value']
        combined=combined.drop_duplicates(identity,keep='last').sort_values(['available_at','feature','period_end'])
        conflicting=combined.groupby(['ticker','feature','available_at'])['value'].nunique()
        if conflicting.gt(1).any():
            raise ValueError('Conflicting event vintages at same availability time; reconcile source versions before export')
        # Re-fetching an unchanged observation must not reset its receipt age.
        # Compare consecutive versions so a later reversion remains observable.
        previous=combined.groupby(['source','ticker','feature','period_end'],dropna=False)['value'].shift()
        combined=combined[combined.value.ne(previous)].reset_index(drop=True)
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        combined.to_csv(path,index=False)
        return combined
