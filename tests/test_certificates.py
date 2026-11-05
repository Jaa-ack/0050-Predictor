import stat

import pytest


def test_certificate_copy_uses_private_permissions_and_never_overwrites(tmp_path):
    from twquant.credentials import import_certificate,private_certificate_path
    source=tmp_path/'downloaded.p12'
    source.write_bytes(b'\x30\x82\x00\xffexample-certificate-bytes')
    destination=import_certificate(source,project_dir=tmp_path)
    assert destination==tmp_path/'.secrets/fubon/client.p12'
    assert destination.read_bytes()==source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode)==0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode)==0o700
    assert stat.S_IMODE(destination.parent.parent.stat().st_mode)==0o700
    assert private_certificate_path(destination)==destination.resolve()
    with pytest.raises(ValueError,match='exists|overwrite'):
        import_certificate(source,project_dir=tmp_path)


def test_certificate_directory_symlink_is_rejected(tmp_path):
    from twquant.credentials import import_certificate
    outside=tmp_path/'outside'
    outside.mkdir()
    (tmp_path/'.secrets').symlink_to(outside,target_is_directory=True)
    source=tmp_path/'download.p12'
    source.write_bytes(b'\x30\x82certificate')
    with pytest.raises(ValueError,match='symlink'):
        import_certificate(source,project_dir=tmp_path)
    assert not list(outside.iterdir())


def test_public_certificate_permissions_and_empty_or_noncertificate_import_are_rejected(tmp_path):
    from twquant.credentials import import_certificate,private_certificate_path
    path=tmp_path/'client.p12'
    path.write_bytes(b'\x30\x82certificate')
    path.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):
        private_certificate_path(path)
    path.write_bytes(b'')
    with pytest.raises(ValueError,match='empty'):
        import_certificate(path,project_dir=tmp_path)
    wrong=tmp_path/'client.txt'
    wrong.write_text('not a certificate')
    with pytest.raises(ValueError,match='p12|pfx'):
        import_certificate(wrong,project_dir=tmp_path)
