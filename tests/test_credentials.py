import json
import os
import stat

import pytest

from twquant.credentials import initialize_credentials, load_credentials, redact


def test_private_blank_template_and_no_overwrite(tmp_path):
    path=tmp_path/".env"
    assert initialize_credentials(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert 'FUGLE_API_KEY=' in path.read_text()
    assert 'FRED_API_KEY=' in path.read_text()
    path.write_text('FRED_API_KEY=example-local-secret\n')
    assert not initialize_credentials(path)
    assert path.read_text() == 'FRED_API_KEY=example-local-secret\n'


def test_credentials_cannot_print_or_serialize_and_environment_wins(tmp_path,monkeypatch):
    path=tmp_path/".env"
    path.write_text('FRED_API_KEY="example-file-value"\nFMP_API_KEY=\nMALICIOUS=$(touch /tmp/bad)\n')
    path.chmod(0o600)
    monkeypatch.setenv('FRED_API_KEY','example-env-secret')
    creds=load_credentials(path)
    assert creds.get('FRED_API_KEY').get_secret_value() == 'example-env-secret'
    assert 'example-env-secret' not in repr(creds)
    assert 'example-env-secret' not in str(creds.get('FRED_API_KEY'))
    assert creds.status()['FRED_API_KEY'] is True
    assert creds.status()['FMP_API_KEY'] is False
    assert 'MALICIOUS' not in creds.status()
    with pytest.raises(TypeError):
        json.dumps(creds.get('FRED_API_KEY'))


def test_credentials_reject_public_file_and_symlink(tmp_path):
    p=tmp_path/".env"
    p.write_text('FMP_API_KEY=example-key\n')
    p.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):
        load_credentials(p)
    p.chmod(0o600)
    link=tmp_path/"link"
    link.symlink_to(p)
    with pytest.raises(ValueError,match='symlink'):
        load_credentials(link)


def test_recursive_redaction_catches_echoed_secrets_urls_and_encoded_values():
    secret='example-secret/with+symbols'
    from urllib.parse import quote
    payload={'apikey':secret,'nested':[{'unexpected':f'https://host/?x={quote(secret,safe="")}'},
             {'password':'other-credential','Mcp-Session-Id':'session'}]}
    result=json.dumps(redact(payload,[secret]))
    assert secret not in result
    assert quote(secret,safe='') not in result
    assert 'other-credential' not in result
    assert 'session' not in result.lower().replace('mcp-session-id','')


def test_provider_logs_mask_credentials_and_drop_exception_details(caplog):
    from twquant.credentials import install_secret_filters
    import logging
    secret='example-key-to-redact-from-logs'
    install_secret_filters(CredentialsForTest(secret))
    with caplog.at_level(logging.WARNING,logger='urllib3.connectionpool'):
        logging.getLogger('urllib3.connectionpool').warning('request %s',secret)
    assert secret not in caplog.text
    assert '[REDACTED]' in caplog.text


def CredentialsForTest(secret):
    from twquant.credentials import Credentials
    return Credentials({'FRED_API_KEY':secret})
