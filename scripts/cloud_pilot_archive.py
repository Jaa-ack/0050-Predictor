"""Local private gzip archives, bounded parts, checksums and restore verification.

No network requests or deletion of captured data. Remote storage stays disabled.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')


def digest_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024**2),b''):digest.update(block)
    return digest.hexdigest()


def private_json(path,value):
    fd,name=tempfile.mkstemp(dir=path.parent,prefix=path.name+'.',suffix='.tmp')
    try:
        with os.fdopen(fd,'w') as out:os.fchmod(out.fileno(),0o600);json.dump(value,out,allow_nan=False);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


class PartWriter:
    def __init__(self,directory,limit):self.directory=directory;self.limit=limit;self.parts=[];self.handle=None;self.size=0
    def write(self,body):
        view=memoryview(body)
        while view:
            if self.handle is None:
                self.path=self.directory/f'part-{len(self.parts)+1:04d}.gz';self.handle=self.path.open('xb');self.path.chmod(0o600);self.size=0;self.digest=hashlib.sha256()
            count=min(len(view),self.limit-self.size);chunk=view[:count];self.handle.write(chunk);self.digest.update(chunk);self.size+=count;view=view[count:]
            if self.size==self.limit:self.finish()
        return len(body)
    def flush(self):
        if self.handle:self.handle.flush()
    def finish(self):
        if self.handle:
            self.handle.flush();os.fsync(self.handle.fileno());self.handle.close();self.parts.append(dict(name=self.path.name,size_bytes=self.size,sha256=self.digest.hexdigest()));self.handle=None


class PartReader:
    def __init__(self,folder,parts):self.folder=folder;self.parts=iter(parts);self.handle=None
    def read(self,count=-1):
        if count<0:raise ValueError('archive_bounded_read_required')
        chunks=[];left=count
        while left:
            if self.handle is None:
                part=next(self.parts,None)
                if part is None:break
                self.handle=(self.folder/part['name']).open('rb')
            body=self.handle.read(left)
            if body:chunks.append(body);left-=len(body)
            else:self.handle.close();self.handle=None
        return b''.join(chunks)
    def close(self):
        if self.handle:self.handle.close();self.handle=None


def check_parts(folder):
    folder=Path(folder)
    if folder.is_symlink():raise ValueError('archive_path_untrusted')
    manifest=json.loads((folder/'manifest.json').read_text())
    for part in manifest['parts']:
        if not re.fullmatch(r'part-\d{4}\.gz',part['name']):raise ValueError('archive_part_untrusted')
        path=folder/part['name']
        if path.is_symlink() or path.stat().st_size!=part['size_bytes'] or digest_file(path)!=part['sha256']:raise ValueError('archive_checksum_failed')
    return manifest


def verify_archive(folder,*,destination=None):
    folder=Path(folder);manifest=check_parts(folder)
    source=PartReader(folder,manifest['parts']);digest=hashlib.sha256();size=0
    try:
        with gzip.GzipFile(fileobj=source,mode='rb') as stream:
            for block in iter(lambda:stream.read(1024**2),b''):
                size+=len(block)
                if size>manifest['source_size_bytes']:raise ValueError('archive_size_invalid')
                digest.update(block)
                if destination:destination.write(block)
    except (OSError,EOFError):raise ValueError('archive_compression_invalid') from None
    finally:source.close()
    if size!=manifest['source_size_bytes'] or digest.hexdigest()!=manifest['source_sha256']:raise ValueError('archive_restore_checksum_failed')
    return dict(verified=True,size_bytes=size,sha256=digest.hexdigest())


def restore_archive(folder,destination):
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise FileExistsError('archive_restore_target_exists')
    if destination.parent.is_symlink():raise ValueError('archive_restore_directory_untrusted')
    fd,name=tempfile.mkstemp(dir=destination.parent,prefix='restore.',suffix='.tmp');temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as out:
            os.fchmod(out.fileno(),0o600);result=verify_archive(folder,destination=out);out.flush();os.fsync(out.fileno())
        os.link(temporary,destination);return result
    finally:temporary.unlink(missing_ok=True)


def archive(root,*,part_bytes=32*1024**2,free_bytes=None):
    if not 1024<=part_bytes<=32*1024**2:raise ValueError('archive_part_limit_invalid')
    root=Path(root);directory=root/'archives'
    if directory.is_symlink():raise ValueError('archive_directory_untrusted')
    protocol=json.loads((root/'protocol.json').read_text());reserve=protocol.get('minimum_free_bytes',4*1024**3)
    try:watermark=json.loads((root/'receipt_watermark.json').read_text())
    except (OSError,ValueError):watermark={}
    if watermark.get('session_mode'):
        try:merged=json.loads((root/'session_merge_status.json').read_text())
        except (OSError,ValueError):merged={}
        if merged.get('date')!=watermark.get('day') or merged.get('state')!='complete' or merged.get('events_total',0)<watermark.get('events_total',0):
            return dict(state='receipts_pending',archives=[],remote_enabled=False)
        try:backup_state=json.loads((root/'backup_status.json').read_text())
        except (OSError,ValueError):backup_state={}
        if backup_state.get('date')!=watermark.get('day') or backup_state.get('state')!='complete':
            return dict(state='backup_pending',archives=[],remote_enabled=False)
    available=free_bytes or (lambda:shutil.disk_usage(root).free)
    if available()<reserve:return dict(state='storage_reserve_reached',archives=[],remote_enabled=False)
    directory.mkdir(mode=0o700,exist_ok=True);directory.chmod(0o700)
    with (root/'archive.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='already_running',archives=[],remote_enabled=False)
        inputs=[]
        backup=root/'backups/latest.sqlite3'
        if backup.is_file():inputs.append(backup)
        for seal in sorted((root/'receipts').glob('*.seal.json')):
            data=root/'receipts'/seal.name.replace('.seal.json','.twqj')
            if data.is_file():inputs.extend([data,seal])
        if inputs:inputs.append(root/'protocol.json')
        index_path=directory/'index.json'
        try:index=json.loads(index_path.read_text())
        except (OSError,ValueError):index={}
        result=[]
        for source in inputs:
            if source.is_symlink():raise ValueError('archive_source_untrusted')
            before=source.stat()
            identity=str(source.relative_to(root));fingerprint=[before.st_size,before.st_mtime_ns]
            cached=index.get(identity,{})
            if cached.get('fingerprint')==fingerprint and re.fullmatch(r'[A-Za-z0-9_.-]+-[a-f0-9]{24}',cached.get('directory','')):
                target=directory/cached['directory'];manifest=check_parts(target)
                if manifest['source_sha256']==cached.get('sha256'):
                    result.append(str(target.relative_to(root)));continue
            if available()<reserve+before.st_size*1.02:return dict(state='storage_reserve_reached',archives=result,remote_enabled=False)
            staging=Path(tempfile.mkdtemp(dir=directory,prefix='.pending-'));staging.chmod(0o700)
            try:
                parts=PartWriter(staging,part_bytes);source_digest=hashlib.sha256()
                with source.open('rb') as incoming,gzip.GzipFile(fileobj=parts,mode='wb',filename='',mtime=0,compresslevel=1) as compressed:
                    for block in iter(lambda:incoming.read(1024**2),b''):
                        if available()<reserve:raise OSError('archive_storage_reserve_reached')
                        source_digest.update(block);compressed.write(block)
                parts.finish();after=source.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('archive_source_changed')
                source_sha=source_digest.hexdigest();target=directory/(source.name+'-'+source_sha[:24])
                manifest=dict(format='gzip-split-v1',source=str(source.relative_to(root)),source_size_bytes=before.st_size,source_sha256=source_sha,parts=parts.parts,remote_enabled=False)
                private_json(staging/'manifest.json',manifest);verify_archive(staging)
                if target.exists():verify_archive(target)
                else:os.rename(staging,target)
                index[identity]=dict(fingerprint=fingerprint,directory=target.name,sha256=source_sha)
                private_json(index_path,index)
                result.append(str(target.relative_to(root)))
            finally:
                if parts.handle:parts.handle.close()
                if staging.exists():shutil.rmtree(staging)
        value=dict(state='complete',at=datetime.now(TZ).isoformat(),archives=result,remote_enabled=False)
        private_json(root/'archive_status.json',value);return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);args=parser.parse_args();os.umask(0o077)
    try:
        protocol=json.loads((args.root/'protocol.json').read_text());stamp=datetime.now(TZ)
        if stamp.date().isoformat() not in protocol['dates'] or stamp.strftime('%H:%M:%S')<'11:00:00':return 0
        result=archive(args.root);print(json.dumps(dict(state=result['state'],archives=len(result['archives']),remote_enabled=False)));return int(result['state']!='complete')
    except Exception:
        private_json(args.root/'archive_status.json',dict(state='error',at=datetime.now(TZ).isoformat(),code='archive_operation_failed',remote_enabled=False));return 1


if __name__=='__main__':raise SystemExit(main())
