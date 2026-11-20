import hashlib
from io import BytesIO
import struct

import pytest

from theaxiom import storage_settings, zim_source


CONFIG = {"format": "zim", "source_url": "https://download.kiwix.org/zim/zimit/example.zim"}


def fixture_bytes():
    header = struct.pack("<IHH16sIIQQQQIIQ", 0x044D495A, 6, 1, bytes(16), 1, 1,
                         80, 88, 92, 100, 0, 0xffffffff, 104)
    # Synthetic header/checksum fixture, not a readable book.
    body = header + bytes(24)
    return body + hashlib.md5(body, usedforsecurity=False).digest()


@pytest.fixture
def disk(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    (root / "staging").mkdir()
    (root / "library").mkdir()
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(zim_source.pdf_source, "LOCAL_FILESYSTEMS",
                        zim_source.pdf_source.LOCAL_FILESYSTEMS | {"overlay"})
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(root)))
    return root


def test_url_only_install_records_actual_size_and_digest(disk):
    data = fixture_bytes()
    seen = []
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data),
                                         progress=lambda current, total: seen.append((current,total)))
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert artifact["bytes"] == len(data)
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data
    assert seen[-1] == (len(data), None)
    assert list((disk / "staging/zim").iterdir()) == []


@pytest.mark.parametrize("changes,invalid", [
    ({"expected_size_bytes":"1"}, False),
    ({"sha256":"a"*64}, False),
    ({}, True),
])
def test_invalid_or_mismatching_download_is_not_installed(disk, changes, invalid):
    data = fixture_bytes()
    if invalid:
        data = data[:-1] + bytes([data[-1] ^ 1])
    with pytest.raises(ValueError):
        zim_source.install_stream("ready", CONFIG | changes, BytesIO(data))
    assert list((disk / "library/zim").iterdir()) == []
    assert list((disk / "staging/zim").iterdir()) == []


def test_capacity_reserve_is_enforced(disk):
    storage_settings.save_settings(dict(storage_settings.load_settings(), minimum_free_bytes=2**63-1))
    with pytest.raises(ValueError, match="capacity"):
        zim_source.install_stream("ready", CONFIG, BytesIO(fixture_bytes()))


def test_optional_pin_validation():
    assert zim_source.candidate(CONFIG)[1:] == (None, None)
    assert zim_source.candidate(CONFIG | {"expected_size_bytes":"120", "sha256":"A"*64})[1:] == (120, "a"*64)
    with pytest.raises(ValueError):
        zim_source.candidate(CONFIG | {"source_url":"http://example.org/a.zim"})


def test_world_writable_library_directory_does_not_block_zim_install(disk):
    from theaxiom import pdf_source

    library = disk / "library" / "zim"
    library.mkdir()
    library.chmod(0o777)
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    assert (library / artifact["filename"]).read_bytes() == data
    assert library.stat().st_mode & 0o777 == 0o777

    # A permissive directory does not weaken ownership/mode checks on files.
    (library / artifact["filename"]).chmod(0o666)
    with pdf_source._directory("library", format_name="zim") as (directory, mount, _):
        with pytest.raises(ValueError, match="unsafe"):
            with pdf_source._file(directory, mount, artifact["filename"], artifact["bytes"]):
                pass
