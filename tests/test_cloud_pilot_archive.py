import importlib.util
import json
from pathlib import Path
import pytest


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_archive.py'
    assert path.exists(),'Private chunked archive support is missing'
    spec=importlib.util.spec_from_file_location('archive',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_chunked_archive_is_private_verified_and_restores_exact_bytes(tmp_path):
    m=module();root=tmp_path/'pilot';root.mkdir();source=root/'backups';source.mkdir();payload=bytes(range(256))*1000
    (source/'latest.sqlite3').write_bytes(payload);(root/'protocol.json').write_text('{}')
    result=m.archive(root,part_bytes=1024,free_bytes=lambda:10**10)
    folder=root/result['archives'][0];manifest=json.loads((folder/'manifest.json').read_text())
    assert len(manifest['parts'])>1 and all(p['size_bytes']<=1024 for p in manifest['parts'])
    assert m.verify_archive(folder)['verified']
    assert all(f.stat().st_mode&0o077==0 for f in folder.iterdir())
    restored=tmp_path/'restored.sqlite3';m.restore_archive(folder,restored);assert restored.read_bytes()==payload
    with pytest.raises(FileExistsError):m.restore_archive(folder,restored)


def test_archive_never_includes_credentials_logs_or_live_database(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'.env').write_text('private-canary');(tmp_path/'pilot.sqlite3').write_text('active');(tmp_path/'sdk.log').write_text('private-canary')
    assert m.archive(tmp_path,free_bytes=lambda:10**10)['archives']==[]
    assert (tmp_path/'pilot.sqlite3').read_text()=='active'


def test_corrupt_archive_cannot_be_marked_verified_or_restored(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'backups').mkdir();(tmp_path/'backups/latest.sqlite3').write_bytes(b'synthetic'*1000)
    result=m.archive(tmp_path,free_bytes=lambda:10**10);folder=tmp_path/result['archives'][0];part=next(folder.glob('part-*'));part.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='archive'):m.verify_archive(folder)
    with pytest.raises(ValueError,match='archive'):m.restore_archive(folder,tmp_path/'restored.sqlite3')
    assert not (tmp_path/'restored.sqlite3').exists()


def test_low_space_preserves_sources_and_existing_archive(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'backups').mkdir();source=tmp_path/'backups/latest.sqlite3';source.write_bytes(b'keep')
    assert m.archive(tmp_path,free_bytes=lambda:1)['state']=='storage_reserve_reached'
    assert source.read_bytes()==b'keep'


def test_archive_waits_for_the_full_history_instead_of_only_a_projection(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'backups').mkdir();(tmp_path/'backups/latest.sqlite3').write_bytes(b'previous')
    (tmp_path/'receipt_watermark.json').write_text('{"day":"2026-11-18","session_mode":true,"events_total":50}')
    result=m.archive(tmp_path,free_bytes=lambda:10**10)
    assert result['state']=='receipts_pending' and not result['remote_enabled']
    assert not (tmp_path/'archives/index.json').exists()


def test_archive_waits_for_a_backup_of_the_rebuilt_day(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'backups').mkdir();(tmp_path/'backups/latest.sqlite3').write_bytes(b'previous')
    (tmp_path/'receipt_watermark.json').write_text('{"day":"2026-11-18","session_mode":true,"events_total":50}')
    (tmp_path/'session_merge_status.json').write_text('{"date":"2026-11-18","state":"complete","events_total":50}')
    result=m.archive(tmp_path,free_bytes=lambda:10**10)
    assert result['state']=='backup_pending'
    (tmp_path/'backup_status.json').write_text('{"date":"2026-11-18","state":"complete"}')
    assert m.archive(tmp_path,free_bytes=lambda:10**10)['state']=='complete'


def test_unchanged_sources_do_not_repeat_compression_or_decompression(tmp_path,monkeypatch):
    m=module();(tmp_path/'protocol.json').write_text('{}');(tmp_path/'backups').mkdir();(tmp_path/'backups/latest.sqlite3').write_bytes(b'synthetic'*1000)
    first=m.archive(tmp_path,free_bytes=lambda:10**10)
    def forbidden(*a,**k):raise AssertionError('Unchanged verified bytes must not be recompressed or inflated')
    monkeypatch.setattr(m.gzip,'GzipFile',forbidden)
    assert m.archive(tmp_path,free_bytes=lambda:10**10)['archives']==first['archives']
