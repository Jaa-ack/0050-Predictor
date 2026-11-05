import json

import pandas as pd
import pytest

from twquant.source_store import SourceStore
from twquant.sources import SourceResult


def test_source_archive_redacts_snapshots_and_export_is_pit_ready(tmp_path):
    store=SourceStore(tmp_path/'sources')
    result=SourceResult('fred','CPIAUCSL','macro',[
        {'date':'2024-01-01','realtime_start':'2024-02-13','value':'100'}],
        '2026-11-04T12:00:00Z',{'api_key':'example-real-secret'})
    one=store.write(result,secrets=['example-real-secret'])
    two=store.write(result,secrets=['example-real-secret'])
    assert one!=two
    assert all('example-real-secret' not in p.read_text() for p in tmp_path.rglob('*.json'))
    e=store.export_events(tmp_path/'events.csv')
    assert len(e)==1
    assert pd.read_csv(tmp_path/'events.csv').ticker.tolist()==['*']
    from twquant.features import point_in_time_join
    decisions=pd.DataFrame({'ticker':['2330','2330'],'decision_ts':pd.to_datetime(['2024-02-13T08:30:00Z','2024-02-15T08:30:00Z'])})
    joined=point_in_time_join(decisions,e)
    assert pd.isna(joined.f_ext_fred_CPIAUCSL.iloc[0])
    assert joined.f_ext_fred_CPIAUCSL.iloc[1]==100
    assert len(store.manifests())==2
    path=tmp_path/'sources'/'snapshots'/one/'result.json'
    path.write_text('{}')
    with pytest.raises(ValueError,match='checksum'):
        store.read(one)


def test_source_snapshot_identifiers_do_not_escape_root(tmp_path):
    with pytest.raises(ValueError,match='snapshot'):
        SourceStore(tmp_path).read('../../elsewhere')
