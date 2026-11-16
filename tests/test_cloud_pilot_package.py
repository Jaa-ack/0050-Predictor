import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest


def module():
    path=Path(__file__).parents[1]/'scripts/prepare_cloud_pilot.py'
    assert path.exists(), 'Linux cloud deployment preparation is missing'
    spec=importlib.util.spec_from_file_location('cloud_prepare',path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


def fixture(tmp_path):
    workspace=tmp_path/'workspace';root=workspace/'artifacts/pilot';source=root/'source_snapshot/src/twquant'
    source.mkdir(parents=True);(source/'__init__.py').write_text('')
    (source/'live_market.py').write_text('print("public source")\n')
    digest=hashlib.sha256()
    for p in sorted(source.glob('*.py')):digest.update(p.name.encode());digest.update(p.read_bytes())
    (root/'protocol.json').write_text(json.dumps(dict(source_sha256=digest.hexdigest(),dates=['2026-11-16','2026-11-17'])))
    (workspace/'pyproject.toml').write_text('[project]\nname="test-project"\n')
    (workspace/'README.md').write_text('Public project documentation\n')
    license=workspace/'docs/licenses/TRADER_SKILLS_MIT.txt';license.parent.mkdir(parents=True);license.write_text('License\n')
    (workspace/'.env').write_text('SECRET=NEVER-PACKAGE\n');(root/'pilot.sqlite3').write_text('PRIVATE-MARKET-DATA')
    return workspace,root


def test_cloud_bundle_contains_only_frozen_source_and_public_metadata(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'bundle'
    result=m.prepare(workspace,root,out)
    with tarfile.open(out/'source.tar.gz') as archive:
        names=archive.getnames();contents=b''.join(archive.extractfile(n).read() for n in names)
    assert 'src/twquant/live_market.py' in names and 'pyproject.toml' in names
    assert b'NEVER-PACKAGE' not in contents and b'PRIVATE-MARKET-DATA' not in contents
    assert not any('protocol' in n or '.env' in n or 'sqlite' in n for n in names)
    assert result['credentials_included'] is False and result['cloud_resources_created'] is False


def test_systemd_jobs_have_exact_dates_and_independent_forecast_preheating(tmp_path):
    m=module();units=m.units('/opt/twquant-pilot','twquant',['2026-11-16','2026-11-17'])
    timer=units['twquant-pilot-forecast.timer']
    assert '2026-11-16 09:59:00 Asia/Taipei' in timer
    assert '2026-11-17 09:59:00 Asia/Taipei' in timer
    assert 'RandomizedDelaySec=0' in timer and 'AccuracySec=1us' in timer
    service=units['twquant-pilot-forecast.service']
    assert '--mode forecast' in service and 'TWQUANT_PILOT_MANAGED=0' in service
    assert 'caffeinate' not in '\n'.join(units.values()) and 'launchctl' not in '\n'.join(units.values())
    assert 'ConditionPathExists=!/opt/twquant-pilot/runtime/cloud_complete' in service
    assert 'OnUnitInactiveSec=60s' in units['twquant-pilot-collect.timer']
    assert 'OnSuccess=twquant-pilot-finalize.service' in units['twquant-pilot-report.service']
    assert 'disable --now twquant-pilot-collect.timer twquant-pilot-forecast.timer twquant-pilot-report.timer' in units['twquant-pilot-finalize.service']


def test_changed_frozen_source_is_rejected_before_output_creation(tmp_path):
    m=module();workspace,root=fixture(tmp_path)
    (root/'source_snapshot/src/twquant/live_market.py').write_text('print("changed")\n')
    output=tmp_path/'rejected'
    with pytest.raises(ValueError,match='fingerprint|source'):
        m.prepare(workspace,root,output)
    assert not output.exists()


def test_existing_debian_python_is_selected_without_system_python_upgrade(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'debian-bundle'
    result=m.prepare(workspace,root,out,python='python3.11')
    script=(out/'install.sh').read_text()
    assert 'command -v python3.11' in script
    assert "python3.11 -m venv" in script
    assert 'python3.12' not in script and 'apt-get' not in script
    assert result['python_command']=='python3.11'
    assert '--without-pip' in script and '--ignore-installed' in script
    assert 'bootstrap_pip_sha256' in result


def test_linux_deployment_requests_native_websocket_validation_without_skipping_checks(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'accelerated'
    m.prepare(workspace,root,out,python='python3.11')
    script=(out/'install.sh').read_text()
    assert '/app[cloud]' in script
    assert 'skip_utf8_validation' not in script
    assert (out/'sqlite_tuning.py').is_file()
    assert 'twquant_cloud_sqlite.install()' in script
    import tomllib
    project=tomllib.loads((Path(__file__).parents[1]/'pyproject.toml').read_text())
    assert any(item.startswith('wsaccel==0.6.7') for item in project['project']['optional-dependencies']['cloud'])


def test_unsupported_python_is_rejected_before_writing_bundle(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'unsupported'
    with pytest.raises(ValueError,match='Python|python'):
        m.prepare(workspace,root,out,python='python3.14')
    assert not out.exists()


def test_shared_host_profile_limits_only_pilot_memory_and_uses_supervisor(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'bounded'
    m.prepare(workspace,root,out,python='python3.11',memory_max='512M')
    assert 'MemoryMax=512M' in (out/'units/twquant-pilot.slice').read_text()
    collector=(out/'units/twquant-pilot-collect.service').read_text()
    assert 'Slice=twquant-pilot.slice' in collector and 'resource_guard.py' in collector
    assert (out/'resource_guard.py').exists()
    assert 'report-ready' in (out/'units/twquant-pilot-report.service').read_text()
    assert '--cloud-runtime' in collector
    assert (out/'collector_runtime.py').is_file()


def test_shared_host_collection_and_report_yield_cpu_without_delaying_forecast():
    generated=module().units('/opt/twquant-pilot','twquant',['2026-11-16'],memory_max='512M')
    assert 'Nice=10' in generated['twquant-pilot-collect.service']
    assert 'Nice=10' in generated['twquant-pilot-report.service']
    assert 'Nice=10' not in generated['twquant-pilot-forecast.service']


def test_cloud_collector_has_time_to_drain_and_report_quiet_begins_before_capture():
    generated=module().units('/opt/twquant-pilot','twquant',['2026-11-16'],memory_max='640M')
    assert 'TimeoutStopSec=150' in generated['twquant-pilot-collect.service']
    assert '2026-11-16 09:28:00 Asia/Taipei' in generated['twquant-pilot-quiet.timer']


def test_shared_host_stops_already_running_reports_before_forecast():
    generated=module().units('/opt/twquant-pilot','twquant',['2026-11-16'],memory_max='512M')
    service=generated['twquant-pilot-quiet.service']
    assert '--phase report-quiet' in service
    assert 'ExecStart=/bin/systemctl stop twquant-pilot-report.service' in service
    assert 'twquant-pilot-collect.service' not in service and 'twquant-pilot-forecast.service' not in service
    assert '2026-11-16 09:28:00 Asia/Taipei' in generated['twquant-pilot-quiet.timer']
    assert 'twquant-pilot-quiet.timer' in generated['twquant-pilot-finalize.service']


def test_existing_host_memory_budget_covers_overlapping_workers_and_remains_bounded():
    profile=json.loads((Path(__file__).parents[1]/'configs/live_pilot_existing_host.json').read_text())
    assert profile['memory_max']=='640M'
    generated=module().units('/opt/twquant-pilot','twquant',['2026-11-16'],memory_max=profile['memory_max'])
    assert 'MemoryMax=640M' in generated['twquant-pilot.slice']
    assert profile['minimum_free_bytes']==4*1024**3


def test_cloud_operations_bundle_has_bounded_monitor_backup_and_preserves_telegram(tmp_path):
    m=module();workspace,root=fixture(tmp_path);out=tmp_path/'operations'
    result=m.prepare(workspace,root,out,python='python3.11',memory_max='512M')
    assert (out/'watchdog.py').is_file()
    assert 'watchdog.py' in result['support_files']
    monitor=(out/'units/twquant-pilot-watchdog.service').read_text()
    assert '--phase check' in monitor and 'NoNewPrivileges=true' in monitor
    assert 'OnUnitInactiveSec=30s' in (out/'units/twquant-pilot-watchdog.timer').read_text()
    backup=(out/'units/twquant-pilot-backup.timer').read_text()
    assert '2026-11-16 10:20:00 Asia/Taipei' in backup
    assert '2026-11-16 08:40:00 Asia/Taipei' in (out/'units/twquant-pilot-preflight.timer').read_text()
    finalize=(out/'units/twquant-pilot-finalize.service').read_text()
    assert 'twquant-pilot-backup.timer' in finalize
    assert 'telegram' not in finalize and 'docker' not in finalize


def test_final_report_marker_is_idempotent_and_preserves_existing_evidence(tmp_path):
    m=module();root=tmp_path/'runtime';root.mkdir()
    (root/'evaluation.json').write_text(json.dumps({'final':True}))
    import subprocess,sys
    helper=tmp_path/'finish.py';helper.write_text(m.FINISH)
    first=subprocess.run([sys.executable,str(helper),str(root)],capture_output=True)
    assert first.returncode==0
    marker=root/'cloud_complete';original=marker.stat().st_mtime_ns
    second=subprocess.run([sys.executable,str(helper),str(root)],capture_output=True)
    assert second.returncode==0 and marker.stat().st_mtime_ns==original


@pytest.mark.parametrize('prefix,user',[('/opt/path\nExecStart=evil','twquant'),('/opt/path%unsafe','twquant'),('/opt/path','bad user')])
def test_unit_arguments_cannot_inject_systemd_directives(prefix,user):
    with pytest.raises(ValueError):module().units(prefix,user,['2026-11-16'])
