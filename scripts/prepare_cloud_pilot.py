"""Prepare a credential-free Linux deployment; never creates cloud resources."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

SDK_SHA256='065c636ea58e00db9fe67aa6ba0936b234ac131da0d4697e9c6f33dbe42bc1ba'
PIP_SHA256='98436feffb9e31bc9339cf369fd55d3331b1580b6a6f1173bacacddcf9c34754'


def units(prefix,user,dates,*,memory_max=None):
    if (not re.fullmatch(r'/[A-Za-z0-9_/-]+',prefix) or '..' in prefix.split('/')
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}',user)
            or not 1<=len(dates)<=10 or dates!=sorted(set(dates))
            or any(not re.fullmatch(r'20\d{2}-\d{2}-\d{2}',d) for d in dates)):
        raise ValueError('Declare bounded dates and trusted Linux paths/user')
    if memory_max is not None and not re.fullmatch(r'[1-9][0-9]*(?:M|G)',memory_max):raise ValueError('Invalid pilot memory limit')
    result={}
    for mode,clock in [('collect','08:45:00'),('forecast','09:59:00'),('report','13:45:00')]:
        if memory_max and mode=='forecast':clock='09:57:00'
        runtime=prefix+'/runtime'
        post=(f'ExecStartPost={prefix}/venv/bin/python {prefix}/finish.py {runtime}\n'
            + f'ExecStartPost={prefix}/venv/bin/python {prefix}/resource_guard.py --root {runtime} --phase report-record\n') if mode=='report' else ''
        success='OnSuccess=twquant-pilot-finalize.service\n' if mode=='report' else ''
        guard=(f'ExecCondition={prefix}/venv/bin/python {prefix}/resource_guard.py --root {runtime} --phase report-ready\n'
            if mode=='report' else '')
        command=(f'{prefix}/venv/bin/python {prefix}/resource_guard.py --root {runtime} --phase collect --cloud-runtime --receipt-journal'
            if mode=='collect' and memory_max else f'{prefix}/venv/bin/python -m twquant.live_market --root {runtime} --mode {mode}')
        if mode=='forecast' and memory_max:command=f'{prefix}/venv/bin/python {prefix}/receipt_journal.py --root {runtime} --source {prefix}/app/src/twquant --sessions --phase forecast'
        result[f'twquant-pilot-{mode}.service']=f'''[Unit]
Description=Bounded 0050 paper pilot {mode}
After=network-online.target time-sync.target{' twquant-pilot-backup.service' if mode=='report' else ''}
Wants=network-online.target
ConditionPathExists={runtime}/protocol.json
ConditionPathExists={prefix}/.env
ConditionPathExists=!{runtime}/cloud_complete
{success}

[Service]
Type={'oneshot' if mode=='report' else 'simple'}
User={user}
{'Slice=twquant-pilot.slice' if memory_max else ''}
{'Nice=10' if memory_max and mode in ('collect','report') else ''}
WorkingDirectory={prefix}
UMask=0077
Environment=TZ=Asia/Taipei
Environment=PYTHONPATH={prefix}/app/src
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=LOKY_MAX_CPU_COUNT=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
Environment=NUMEXPR_NUM_THREADS=1
Environment=TWQUANT_PILOT_MANAGED=0
{guard}ExecStart={command}
{post}TimeoutStartSec={'300' if mode=='report' else 'infinity'}
RuntimeMaxSec={'21600' if mode=='collect' else '480' if mode=='forecast' and memory_max else '180' if mode=='forecast' else '600'}
{'CPUWeight=1000' if mode=='forecast' and memory_max else ''}
TimeoutStopSec={'150' if memory_max and mode=='collect' else '30'}
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={runtime} {prefix}/log
StandardOutput=append:{runtime}/{mode}.log
StandardError=append:{runtime}/{mode}.log
'''
        calendar='\n'.join(f'OnCalendar={d} {clock} Asia/Taipei' for d in dates)
        result[f'twquant-pilot-{mode}.timer']=f'''[Unit]
Description=Ten declared sessions only: 0050 {mode}

[Timer]
{calendar}
AccuracySec=1us
RandomizedDelaySec=0
Persistent=true
Unit=twquant-pilot-{mode}.service
{'OnBootSec=2min' if mode in ('collect','report') else ''}
{'OnUnitInactiveSec=60s' if mode=='collect' else 'OnUnitInactiveSec=300s' if mode=='report' else ''}

[Install]
WantedBy=timers.target
'''
    checkpoint_retire=('ExecStart=/bin/systemctl disable --now twquant-pilot-checkpoint.timer\nExecStart=/bin/systemctl stop twquant-pilot-checkpoint.service\n' if memory_max else '')
    journal_retire=('ExecStart=/bin/systemctl disable --now twquant-pilot-replay.timer twquant-pilot-archive.timer\nExecStart=/bin/systemctl stop twquant-pilot-replay.service twquant-pilot-archive.service\n' if memory_max else '')
    result['twquant-pilot-finalize.service']=f'''[Unit]
Description=Retire only this completed paper pilot
ConditionPathExists={prefix}/runtime/cloud_complete

[Service]
Type=oneshot
User=root
ExecStart=/bin/systemctl disable --now twquant-pilot-collect.timer twquant-pilot-forecast.timer twquant-pilot-report.timer
ExecStart=/bin/systemctl stop twquant-pilot-collect.service twquant-pilot-forecast.service
ExecStart=/bin/systemctl disable --now twquant-pilot-backup.timer
ExecStart=/bin/systemctl stop twquant-pilot-backup.service
ExecStart=/bin/systemctl disable --now twquant-pilot-preflight.timer
ExecStart=/bin/systemctl stop twquant-pilot-preflight.service
ExecStart=/bin/systemctl disable --now twquant-pilot-quiet.timer
{checkpoint_retire}{journal_retire}ExecStart=/bin/systemctl start --no-block twquant-pilot-watchdog.service
'''
    if memory_max:
        result['twquant-pilot.slice']=f'''[Unit]
Description=Memory budget for this pilot only

[Slice]
MemoryAccounting=true
MemoryMax={memory_max}
'''
    result.update(operations_units(prefix,user,dates,quiet_clock='08:38:00' if memory_max else '09:58:00'))
    if memory_max:
        result['twquant-pilot-checkpoint.service']=f'''[Unit]
Description=Bounded PASSIVE WAL checkpoint outside protected forecast windows
ConditionPathExists={prefix}/runtime/protocol.json
ConditionPathExists=!{prefix}/runtime/cloud_complete
[Service]
Type=oneshot
User={user}
Slice=twquant-pilot.slice
Nice=19
UMask=0077
Environment=TZ=Asia/Taipei
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={prefix}/venv/bin/python {prefix}/checkpoint.py --root {prefix}/runtime
ExecStopPost={prefix}/venv/bin/python {prefix}/checkpoint.py --root {prefix}/runtime --interrupted
TimeoutStartSec=45
TimeoutStopSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={prefix}/runtime
'''
        calendar='\n'.join(f'OnCalendar={d} 09:30:00 Asia/Taipei' for d in dates)
        result['twquant-pilot-checkpoint.timer']='[Unit]\nDescription=Bounded WAL maintenance\n[Timer]\n'+calendar+'\nOnUnitInactiveSec=60s\nAccuracySec=1s\nUnit=twquant-pilot-checkpoint.service\n[Install]\nWantedBy=timers.target\n'
        for kind,clock in [('replay','10:18:00'),('archive','11:00:00')]:
            command=(f'{prefix}/venv/bin/python {prefix}/receipt_journal.py --root {prefix}/runtime --source {prefix}/app/src/twquant --sessions --phase replay'
                if kind=='replay' else f'/usr/bin/python3 {prefix}/archive.py --root {prefix}/runtime')
            result[f'twquant-pilot-{kind}.service']=f'''[Unit]
Description=Private pilot {kind} after capture
ConditionPathExists={prefix}/runtime/protocol.json
ConditionPathExists=!{prefix}/runtime/cloud_complete
[Service]
Type=oneshot
User={user}
Slice=twquant-pilot.slice
Nice=15
WorkingDirectory={prefix}
UMask=0077
Environment=TZ=Asia/Taipei
Environment=PYTHONPATH={prefix}/app/src
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart={command}
TimeoutStartSec={1800 if kind=='replay' else 600}
TimeoutStopSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={prefix}/runtime
StandardOutput=append:{prefix}/runtime/{kind}.log
StandardError=append:{prefix}/runtime/{kind}.log
'''
            calendar='\n'.join(f'OnCalendar={d} {clock} Asia/Taipei' for d in dates)
            repeat='OnUnitInactiveSec=60s\n' if kind=='replay' else ''
            result[f'twquant-pilot-{kind}.timer']='[Unit]\nDescription=Private pilot '+kind+'\n[Timer]\n'+calendar+'\n'+repeat+'AccuracySec=1s\nUnit=twquant-pilot-'+kind+'.service\n[Install]\nWantedBy=timers.target\n'
    if memory_max:
        for kind in ['replay','backup','archive','report']:
            name='twquant-pilot-'+kind+'.service';text=result[name]
            command=f'{prefix}/venv/bin/python {prefix}/pipeline.py --root {prefix}/runtime --source {prefix}/app/src/twquant --phase {kind}'
            condition=f'ExecCondition=/usr/bin/python3 {prefix}/pipeline.py --root {prefix}/runtime --source {prefix}/app/src/twquant --phase condition\n'
            text=re.sub(r'^ExecCondition=.*\n','',text,flags=re.M)
            text=re.sub(r'^ExecStartPre=.*\n','',text,flags=re.M)
            text=re.sub(r'^ExecStartPost=.*\n','',text,flags=re.M)
            text=re.sub(r'^ExecStart=.*$',condition+'ExecStart='+command,text,flags=re.M)
            text=re.sub(r'^Restart=.*$','Restart=no',text,flags=re.M)
            text=re.sub(r'^TimeoutStartSec=.*$','TimeoutStartSec=1800',text,flags=re.M)
            text=re.sub(r'^RuntimeMaxSec=.*$','RuntimeMaxSec=1800',text,flags=re.M)
            for key,value in [('PYTHONPATH',prefix+'/app/src'),('OPENBLAS_NUM_THREADS','1'),('OMP_NUM_THREADS','1'),('LOKY_MAX_CPU_COUNT','1')]:
                if 'Environment='+key+'=' not in text:text=text.replace('[Service]\n','[Service]\nEnvironment='+key+'='+value+'\n',1)
            if 'OnSuccess=' not in text:text=text.replace('[Unit]\n','[Unit]\nOnSuccess=twquant-pilot-finalize.service\n',1)
            result[name]=text
        result['twquant-pilot-quiet.service']=result['twquant-pilot-quiet.service'].replace('stop twquant-pilot-report.service','stop twquant-pilot-report.service twquant-pilot-replay.service twquant-pilot-backup.service twquant-pilot-archive.service')
        result['twquant-pilot-quiet.service']=re.sub(r'^ExecCondition=.*$',f'ExecCondition=/usr/bin/python3 {prefix}/pipeline.py --root {prefix}/runtime --source {prefix}/app/src/twquant --phase quiet-condition',result['twquant-pilot-quiet.service'],flags=re.M)
        result['twquant-pilot-quiet.service']+=f'\nReadWritePaths={prefix}/runtime\nExecStartPost=/usr/bin/python3 {prefix}/watchdog.py --root {prefix}/runtime --prefix {prefix} --phase cleanup\n'
    return result


def operations_units(prefix,user,dates,*,quiet_clock='09:58:00'):
    result={}
    for kind,phase,identity in [('watchdog','check','root'),('backup','backup',user),('preflight','preflight',user)]:
        result[f'twquant-pilot-{kind}.service']=f'''[Unit]
Description=Private bounded 0050 pilot {kind}
After=network-online.target time-sync.target
Wants=network-online.target
ConditionPathExists={prefix}/runtime/protocol.json

[Service]
Type=oneshot
User={identity}
{'Slice=twquant-pilot.slice' if kind=='backup' else ''}
WorkingDirectory={prefix}
UMask=0077
Environment=TZ=Asia/Taipei
Environment=PYTHONDONTWRITEBYTECODE=1
{'ExecStartPre=+/bin/systemctl stop twquant-pilot-report.service' if kind=='backup' else ''}
ExecStart=/usr/bin/python3 {prefix}/watchdog.py --root {prefix}/runtime --prefix {prefix} --phase {phase}
TimeoutStartSec={'1800' if kind=='backup' else '90' if kind=='preflight' else '60'}
Restart={'no' if kind=='preflight' else 'on-failure'}
RestartSec=30
MemoryMax={'384M' if kind=='preflight' else '640M' if kind=='backup' else '64M'}
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={prefix}/runtime {prefix}/log
StandardOutput=append:{prefix}/runtime/{kind}.log
StandardError=append:{prefix}/runtime/{kind}.log
'''
        if kind=='watchdog':
            timing='OnBootSec=45s\nOnUnitInactiveSec=30s'
        else:
            clock='08:40:00' if kind=='preflight' else '10:20:00'
            timing='\n'.join(f'OnCalendar={day} {clock} Asia/Taipei' for day in dates)
        result[f'twquant-pilot-{kind}.timer']=f'''[Unit]
Description=Bounded 0050 pilot {kind} schedule

[Timer]
{timing}
AccuracySec=1s
RandomizedDelaySec=0
Persistent=true
Unit=twquant-pilot-{kind}.service

[Install]
WantedBy=timers.target
'''
    result['twquant-pilot-quiet.service']=f'''[Unit]
Description=Stop an existing research report before the forecast quiet window

[Service]
Type=oneshot
User=root
UMask=0077
ExecCondition=/usr/bin/python3 {prefix}/resource_guard.py --root {prefix}/runtime --phase report-quiet
ExecStart=/bin/systemctl stop twquant-pilot-report.service
TimeoutStartSec=45
MemoryMax=32M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
'''
    calendar='\n'.join(f'OnCalendar={day} {quiet_clock} Asia/Taipei' for day in dates)
    result['twquant-pilot-quiet.timer']=f'''[Unit]
Description=Protect ten declared forecast windows from running reports

[Timer]
{calendar}
AccuracySec=1us
RandomizedDelaySec=0
Persistent=true
Unit=twquant-pilot-quiet.service

[Install]
WantedBy=timers.target
'''
    return result


def installer(prefix,user,archive_sha,python):
    return f'''#!/bin/bash
set -euo pipefail
umask 077
test "$(id -u)" = 0 || {{ echo "Run this installer as root on the approved VM"; exit 1; }}
test "$#" -ge 1 && test "$#" -le 2 || {{ echo "Usage: install.sh sdk.whl [pip-26.0.whl]"; exit 1; }}
bundle_dir="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
sdk_wheel="$1"
test "$(sha256sum "$sdk_wheel" | cut -d ' ' -f 1)" = "{SDK_SHA256}"
test "$(sha256sum "$bundle_dir/source.tar.gz" | cut -d ' ' -f 1)" = "{archive_sha}"
test ! -e '{prefix}/app' || {{ echo "Refusing to overwrite an existing deployment"; exit 1; }}
command -v {python} >/dev/null
needs_bootstrap=false
if ! {python} -c 'import ensurepip' >/dev/null 2>&1; then
    test "$#" = 2 || {{ echo 'This Python requires the verified pip bootstrap wheel'; exit 1; }}
    bootstrap_pip="$2"
    test "$(sha256sum "$bootstrap_pip" | cut -d ' ' -f 1)" = "{PIP_SHA256}"
    needs_bootstrap=true
fi
id '{user}' >/dev/null 2>&1 || useradd --system --home-dir '{prefix}' --shell /usr/sbin/nologin '{user}'
install -d -m 0755 '{prefix}' '{prefix}/app'
install -d -o '{user}' -g '{user}' -m 0700 '{prefix}/runtime' '{prefix}/log' '{prefix}/.secrets' '{prefix}/.secrets/fubon'
tar --no-same-owner -xzf "$bundle_dir/source.tar.gz" -C '{prefix}/app'
if "$needs_bootstrap"; then
    {python} -m venv --without-pip '{prefix}/venv'
    PYTHONPATH="$bootstrap_pip" '{prefix}/venv/bin/python' -m pip install --no-index --ignore-installed "$bootstrap_pip"
else
    {python} -m venv '{prefix}/venv'
fi
'{prefix}/venv/bin/python' -m pip install --no-cache-dir --disable-pip-version-check '{prefix}/app[cloud]' "$sdk_wheel"
site_dir="$('{prefix}/venv/bin/python' -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
install -m 0644 "$bundle_dir/sqlite_tuning.py" "$site_dir/twquant_cloud_sqlite.py"
printf '%s\\n' 'import twquant_cloud_sqlite; twquant_cloud_sqlite.install()' > "$site_dir/twquant_cloud_sqlite.pth"
chmod 0644 "$site_dir/twquant_cloud_sqlite.pth"
# Only public code/packages are made readable to the unprivileged service user.
# The sibling runtime, log and certificate directories retain owner-only modes.
chmod -R a+rX '{prefix}/app' '{prefix}/venv'
install -m 0644 "$bundle_dir/finish.py" '{prefix}/finish.py'
install -m 0644 "$bundle_dir/resource_guard.py" '{prefix}/resource_guard.py'
install -m 0644 "$bundle_dir/collector_runtime.py" '{prefix}/collector_runtime.py'
install -m 0644 "$bundle_dir/watchdog.py" '{prefix}/watchdog.py'
install -m 0644 "$bundle_dir/checkpoint.py" '{prefix}/checkpoint.py'
install -m 0644 "$bundle_dir/receipt_journal.py" '{prefix}/receipt_journal.py'
install -m 0644 "$bundle_dir/archive.py" '{prefix}/archive.py'
install -m 0644 "$bundle_dir/pipeline.py" '{prefix}/pipeline.py'
for file in "$bundle_dir"/units/*; do install -m 0644 "$file" /etc/systemd/system/; done
systemctl daemon-reload
echo 'Installed inactive units. No credentials copied; no timer enabled.'
echo 'After approved private setup, run the cloud access probe before enabling timers.'
'''


FINISH='''from pathlib import Path
import json,os,sys
root=Path(sys.argv[1])
report=json.loads((root/'evaluation.json').read_text())
if report.get('final') is True:
    try:
        fd=os.open(root/'cloud_complete',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        pass
    else:
        os.fsync(fd)
        os.close(fd)
'''


def prepare(workspace,root,output,*,prefix='/opt/twquant-pilot',user='twquant-pilot',python='python3.12',memory_max=None):
    if python not in ('python3.11','python3.12','python3.13'):raise ValueError('Choose a supported Python3.11/3.12/3.13 command')
    workspace=Path(workspace).resolve();root=Path(root).resolve();output=Path(output)
    protocol=json.loads((root/'protocol.json').read_text());source=root/'source_snapshot/src/twquant'
    files=sorted(source.glob('*.py'));digest=hashlib.sha256()
    if not files:raise ValueError('Frozen source snapshot is missing')
    for path in files:
        if path.is_symlink():raise ValueError('Frozen source cannot contain symlinks')
        digest.update(path.name.encode());digest.update(path.read_bytes())
    if digest.hexdigest()!=protocol['source_sha256']:raise ValueError('Frozen source fingerprint differs')
    generated=units(prefix,user,protocol['dates'],memory_max=memory_max)
    guard_source=Path(__file__).with_name('cloud_pilot_runtime.py').read_text()
    watchdog_source=Path(__file__).with_name('cloud_pilot_watchdog.py').read_text()
    sqlite_source=Path(__file__).with_name('cloud_pilot_sqlite.py').read_text()
    collector_source=Path(__file__).with_name('cloud_pilot_collector.py').read_text()
    checkpoint_source=Path(__file__).with_name('cloud_pilot_checkpoint.py').read_text()
    journal_source=Path(__file__).with_name('cloud_receipt_journal.py').read_text()
    archive_source=Path(__file__).with_name('cloud_pilot_archive.py').read_text()
    pipeline_source=Path(__file__).with_name('cloud_pilot_pipeline.py').read_text()
    metadata=[workspace/'pyproject.toml',workspace/'README.md',workspace/'docs/licenses/TRADER_SKILLS_MIT.txt']
    if any(not p.is_file() or p.is_symlink() for p in metadata):raise ValueError('Public package metadata is missing')
    if output.exists():raise ValueError('Deployment preparation requires fresh output')
    output.mkdir(parents=True,mode=0o700);(output/'units').mkdir(mode=0o700)
    manifest=[]
    with tarfile.open(output/'source.tar.gz','w:gz') as archive:
        for path,name in [(p,'src/twquant/'+p.name) for p in files]+[(p,str(p.relative_to(workspace))) for p in metadata]:
            body=path.read_bytes();info=tarfile.TarInfo(name);info.size=len(body);info.mode=0o644
            archive.addfile(info,io.BytesIO(body));manifest.append(dict(path=name,sha256=hashlib.sha256(body).hexdigest()))
    archive_sha=hashlib.sha256((output/'source.tar.gz').read_bytes()).hexdigest()
    for name,text in generated.items():(output/'units'/name).write_text(text)
    (output/'install.sh').write_text(installer(prefix,user,archive_sha,python));(output/'finish.py').write_text(FINISH)
    (output/'resource_guard.py').write_text(guard_source)
    (output/'watchdog.py').write_text(watchdog_source)
    (output/'sqlite_tuning.py').write_text(sqlite_source)
    (output/'collector_runtime.py').write_text(collector_source)
    (output/'checkpoint.py').write_text(checkpoint_source)
    (output/'receipt_journal.py').write_text(journal_source)
    (output/'archive.py').write_text(archive_source)
    (output/'pipeline.py').write_text(pipeline_source)
    result=dict(source_sha256=digest.hexdigest(),archive_sha256=archive_sha,files=manifest,dates=protocol['dates'],
        prefix=prefix,service_user=user,sdk_version='2.4.0',sdk_platform='linux_x86_64',sdk_sha256=SDK_SHA256,
        python_command=python,bootstrap_pip_version='26.0',bootstrap_pip_sha256=PIP_SHA256,
        memory_max=memory_max,
        credentials_included=False,provider_data_included=False,cloud_resources_created=False,units_activated=False)
    (output/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    for path in output.rglob('*'):
        if path.is_file():path.chmod(0o600)
    result['support_files']={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob('*')) if p.is_file() and p.name not in ('source.tar.gz','manifest.json')}
    (output/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path);parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--python',choices=('python3.11','python3.12','python3.13'),default='python3.12')
    parser.add_argument('--memory-max')
    args=parser.parse_args();r=prepare(Path.cwd(),args.root,args.out,python=args.python,memory_max=args.memory_max)
    print(json.dumps({k:v for k,v in r.items() if k!='files'}))


if __name__=='__main__':main()
