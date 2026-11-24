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


@pytest.mark.parametrize("title_pointer", [0, 0xffffffffffffffff, 88])
def test_zim_6_header_accepts_legacy_title_pointer_or_retired_sentinel(title_pointer):
    data = bytearray(fixture_bytes())
    # Title pointer occupies bytes 40..47 in the 80-byte ZIM header.
    struct.pack_into("<Q", data, 40, title_pointer)
    zim_source.check_header(bytes(data[:80]), len(data))


@pytest.mark.parametrize("title_pointer", [1, 79, 101, 0xfffffffffffffffe])
def test_zim_6_header_rejects_invalid_title_pointer(title_pointer):
    data = bytearray(fixture_bytes())
    struct.pack_into("<Q", data, 40, title_pointer)
    with pytest.raises(ValueError, match="Invalid ZIM header or index bounds"):
        zim_source.check_header(bytes(data[:80]), len(data))


def test_zim_6_3_retired_title_pointer_matches_ready_gov_header():
    # The observed Ready.gov ZIM 6.3 header has a retired title-index sentinel.
    header = struct.pack(
        "<IHH16sIIQQQQIIQ", 0x044D495A, 6, 3, bytes(16),
        7991, 831, 2063824816, 0xffffffffffffffff,
        2063888744, 80, 7991, 0xffffffff, 2063895392,
    )
    zim_source.check_header(header, 2063895408)
    with pytest.raises(ValueError, match="Invalid ZIM header or index bounds"):
        zim_source.check_header(header, 2063895407)


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


def test_checksum_reports_passes_and_full_read_before_digest_failure(disk):
    data = fixture_bytes()
    stage = disk / "staging/zim"
    stage.mkdir()
    partial = stage / zim_source.partial_name("ready")
    partial.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    partial.chmod(0o600)
    samples = []

    with pytest.raises(ValueError, match="internal checksum mismatch"):
        zim_source.revalidate_retained("ready", CONFIG, expected_bytes=len(data),
                                      progress=lambda current, total, fields: samples.append(fields))

    boundaries = [(sample["checksum_pass"], sample["checksum_bytes"]) for sample in samples
                  if sample["checksum_bytes"] in (0, len(data))]
    assert boundaries == [
        (1, 0), (1, len(data)), (2, 0), (2, len(data)),
    ]
    assert all(sample["checksum_passes"] == 2 for sample in samples)
    assert partial.exists()
    assert not list((disk / "library/zim").iterdir())


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


def test_actual_resumption_appends_only_requested_range_and_verifies_whole_archive(disk, monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import patch

    data = fixture_bytes()
    half = 72
    call_offsets = []

    class Stream(BytesIO):
        def __init__(self, chunk, total):
            super().__init__(chunk)
            self.total_size = total
            self.etag = '"original"'
            self.last_modified = None
            self.length = len(chunk)

    @contextmanager
    def remote(url, *, offset=0, validator=None, **kwargs):
        call_offsets.append((offset, validator))
        assert validator == ('"original"' if offset else None)
        yield Stream(data[offset:], len(data))

    progress = []
    with patch.object(zim_source.pdf_transport, "open_pdf", remote):
        def pause_after_chunk(n, total, metadata):
            progress.append(n)
            if n >= half:
                raise zim_source.TransferStopped()

        # First request writes its initial block before a deliberate interruption.
        original_chunk = zim_source.CHUNK
        monkeypatch.setattr(zim_source, "CHUNK", half)
        with pytest.raises(zim_source.TransferStopped):
            zim_source.download_resumable("ready", CONFIG, progress=pause_after_chunk)
        assert (disk / "staging/zim/.download-ready.partial").read_bytes() == data[:half]
        artifact = zim_source.download_resumable("ready", CONFIG, offset=half,
                   validator='"original"')
    assert call_offsets == [(0, None), (half, '"original"')]
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data
    assert not (disk / "staging/zim/.download-ready.partial").exists()


def test_resumption_validator_change_retains_partial(disk):
    from contextlib import contextmanager
    from unittest.mock import patch

    data = fixture_bytes()
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    part = stage / ".download-ready.partial"
    part.write_bytes(data[:40])
    part.chmod(0o600)

    class Stream(BytesIO):
        total_size = len(data)
        etag = '"changed"'
        last_modified = None

    @contextmanager
    def remote(url, **kwargs):
        yield Stream(data[40:])

    with patch.object(zim_source.pdf_transport, "open_pdf", remote):
        with pytest.raises(ValueError, match="validator changed"):
            zim_source.download_resumable("ready", CONFIG, offset=40, validator='"old"')
    assert part.read_bytes() == data[:40]
    assert not list((disk / "library/zim").iterdir())


def test_resume_refuses_inconsistent_saved_offset(disk):
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    part = stage / ".download-ready.partial"
    part.write_bytes(fixture_bytes()[:32])
    part.chmod(0o600)
    with pytest.raises(ValueError, match="offset disagrees"):
        zim_source.download_resumable("ready", CONFIG, offset=40, validator='"old"')
    assert part.stat().st_size == 32


def test_world_writable_staging_does_not_allow_swapped_inode_promotion(disk, monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import patch

    data = fixture_bytes()
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    stage.chmod(0o777)
    library = disk / "library/zim"
    library.mkdir(exist_ok=True)
    library.chmod(0o777)

    class Stream(BytesIO):
        length = len(data)
        total_size = len(data)
        etag = '"same"'
        last_modified = None

    @contextmanager
    def remote(url, **kwargs):
        yield Stream(data)

    original = zim_source.os.link
    def swap_before_link(source, dest, *, src_dir_fd, dst_dir_fd, follow_symlinks):
        zim_source.os.unlink(source, dir_fd=src_dir_fd)
        fd = zim_source.os.open(source, zim_source.os.O_CREAT | zim_source.os.O_EXCL |
                                zim_source.os.O_WRONLY, 0o600, dir_fd=src_dir_fd)
        with zim_source.os.fdopen(fd, "wb") as writer:
            writer.write(b"malicious substitute")
        return original(source, dest, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                        follow_symlinks=follow_symlinks)

    with patch.object(zim_source.pdf_transport, "open_pdf", remote):
        with patch.object(zim_source.os, "link", swap_before_link):
            with pytest.raises(ValueError, match="changed during final promotion"):
                zim_source.download_resumable("ready", CONFIG)

    assert not list(library.iterdir())
    assert stage.stat().st_mode & 0o777 == 0o777
    assert library.stat().st_mode & 0o777 == 0o777


@pytest.mark.parametrize("interrupt", [False, True])
def test_short_network_reads_batch_storage_writes_and_resume(disk, monkeypatch, interrupt):
    from contextlib import contextmanager

    # Keep the fixture small while exercising the same batching ratio.
    chunk = 64
    data = fixture_bytes()
    monkeypatch.setattr(zim_source, "CHUNK", chunk)
    reads = []
    locks = []
    offsets = []
    progress = []
    cancel = False

    class Stream(BytesIO):
        total_size = len(data)
        etag = '"stable"'
        last_modified = None

        def read(self, size=-1):
            nonlocal cancel
            block = super().read(min(size, 4))
            if block:
                reads.append(len(block))
                if interrupt and len(reads) == 18:
                    cancel = True
            return block

    @contextmanager
    def remote(url, *, offset=0, validator=None, **kwargs):
        offsets.append(offset)
        assert validator == ('"stable"' if offset else None)
        yield Stream(data[offset:])

    @contextmanager
    def write_lock():
        locks.append(len(reads))
        yield

    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf", remote)
    kwargs = dict(write_lock=write_lock, stopped=lambda: cancel,
                  progress=lambda n, total, metadata: progress.append(n))
    if interrupt:
        with pytest.raises(zim_source.TransferStopped):
            zim_source.download_resumable("ready", CONFIG, **kwargs)
        partial = disk / "staging/zim/.download-ready.partial"
        assert partial.read_bytes() == data[:chunk]
        assert locks == [16]
        assert progress[-1] == chunk
        cancel = False
        artifact = zim_source.download_resumable(
            "ready", CONFIG, offset=chunk, validator='"stable"', **kwargs)
        assert offsets == [0, chunk]
        assert locks == [16, 32]
    else:
        artifact = zim_source.download_resumable("ready", CONFIG, **kwargs)
        assert offsets == [0]
        assert locks == [16, 30]
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data
    assert not (disk / "staging/zim/.download-ready.partial").exists()
