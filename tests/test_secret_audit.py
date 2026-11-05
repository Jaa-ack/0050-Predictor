from pathlib import Path

from twquant.credentials import Credentials
from twquant.secret_audit import audit_repository


def test_audit_detects_hardcoded_key_without_echoing_it(tmp_path):
    secret='example-sensitive-value-938495847'
    (tmp_path/'settings.py').write_text('FRED_API_KEY="'+secret+'"\n')
    result=audit_repository(tmp_path,Credentials({'FRED_API_KEY':secret}))
    assert not result['passed']
    assert secret not in str(result)
    assert any(f['path']=='settings.py' for f in result['findings'])


def test_private_local_env_not_publication_candidate_but_certificate_is(tmp_path):
    env=tmp_path/'.env'
    env.write_text('FRED_API_KEY=example-local-private-value\n')
    env.chmod(0o600)
    (tmp_path/'.env.example').write_text('FRED_API_KEY=\n')
    (tmp_path/'README.md').write_text('Set FRED_API_KEY in .env')
    assert audit_repository(tmp_path)['passed']
    (tmp_path/'certificate.pfx').write_bytes(b'not-real-certificate')
    result=audit_repository(tmp_path)
    assert not result['passed']
    assert any(f['rule']=='private_credential_file' for f in result['findings'])


def test_scanner_code_mentioning_private_key_marker_is_not_key_material(tmp_path):
    marker='-----BEGIN '+'PRIVATE KEY-----'
    (tmp_path/'scanner.py').write_text('if '+repr(marker)+' in line:\n    pass\n')
    assert audit_repository(tmp_path)['passed']


def test_git_force_added_private_env_is_still_blocked(tmp_path):
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'.gitignore').write_text('.env\n*.pfx\n')
    (tmp_path/'.env').write_text('FRED_API_KEY=example-force-added-private\n')
    subprocess.run(['git','add','--force','.env'],cwd=tmp_path,check=True,capture_output=True)
    result=audit_repository(tmp_path)
    assert not result['passed']
    assert any(f['rule']=='tracked_secret_file' for f in result['findings'])
    assert 'example-force-added-private' not in str(result)


def test_git_force_added_runtime_data_is_blocked_even_when_binary(tmp_path):
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'artifacts').mkdir()
    (tmp_path/'artifacts'/'model.joblib').write_bytes(b'\x80\x04\x95\xff')
    subprocess.run(['git','add','artifacts/model.joblib'],cwd=tmp_path,check=True,capture_output=True)
    result=audit_repository(tmp_path)
    assert not result['passed']
    assert any(f['rule']=='tracked_runtime_artifact' for f in result['findings'])


def test_private_certificate_directory_is_excluded_but_force_added_cert_is_blocked(tmp_path):
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    private=tmp_path/'.secrets/fubon'
    private.mkdir(parents=True)
    cert=private/'client.p12'
    cert.write_bytes(b'\x30\x82\x00\xff')
    cert.chmod(0o600)
    (tmp_path/'.gitignore').write_text('.secrets/\n*.p12\n')
    assert audit_repository(tmp_path)['passed']
    subprocess.run(['git','add','--force','.secrets/fubon/client.p12'],cwd=tmp_path,check=True,capture_output=True)
    result=audit_repository(tmp_path)
    assert not result['passed']
    assert any(x['rule']=='private_credential_file' for x in result['findings'])


def test_native_sdk_logs_are_private_but_force_tracked_logs_are_blocked(tmp_path):
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    secret='example-private-sdk-credential'
    folder=tmp_path/'log';folder.mkdir()
    (folder/'sdk.20261105').write_text(secret)
    credentials=Credentials({'FUBON_API_KEY':secret})
    assert audit_repository(tmp_path,credentials)['passed']
    subprocess.run(['git','add','log/sdk.20261105'],cwd=tmp_path,check=True,capture_output=True)
    result=audit_repository(tmp_path,credentials)
    assert not result['passed']
    assert any(row['rule']=='tracked_runtime_artifact' for row in result['findings'])
    assert secret not in str(result)
