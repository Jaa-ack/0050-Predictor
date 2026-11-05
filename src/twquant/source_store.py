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
        return provider in {'fred','finmind'} or provider=='fmp' and (dataset in {'prices','adjusted_prices'} or kind=='financial' and dataset!='estimates')

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
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        combined.to_csv(path,index=False)
        return combined
