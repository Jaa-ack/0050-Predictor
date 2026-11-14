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


@pytest.mark.parametrize('prefix,user',[('/opt/path\nExecStart=evil','twquant'),('/opt/path%unsafe','twquant'),('/opt/path','bad user')])
def test_unit_arguments_cannot_inject_systemd_directives(prefix,user):
    with pytest.raises(ValueError):module().units(prefix,user,['2026-11-16'])
