"""Regression tests for durable ZIM lifecycle and safe cross-process serialization."""
from io import BytesIO
import threading
from unittest.mock import patch

import pytest

from theaxiom import zim_jobs, zim_source
from test_zim_source import CONFIG, disk, fixture_bytes


def join_worker():
    for thread in threading.enumerate():
        if thread.name in {"zim-acquisition", "zim-local-revalidation"}:
            thread.join(5)
            assert not thread.is_alive()


def test_job_records_url_only_and_rejects_concurrent_start(disk):
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, progress, stopped, **kwargs):
        entered.set()
        assert release.wait(5)
        return zim_source.install_stream(sid, config, BytesIO(fixture_bytes()),
                                         progress=lambda n, total: progress(n, total, {}))

    with patch.object(zim_source, "download_resumable", download):
        try:
            assert zim_jobs.start("ready", CONFIG)["accepted"]
            assert entered.wait(5)
            job = zim_jobs.status("ready")
            assert job["active"] and job["configuration"]["sha256"] == ""
            with pytest.raises(BlockingIOError):
                zim_jobs.start("other", CONFIG)
        finally:
            release.set()
        join_worker()
    job = zim_jobs.status("ready")
    assert job["state"] == "completed"
    assert zim_jobs.installed(job, CONFIG)["bytes"] == len(fixture_bytes())
    assert not zim_jobs.installed(job, CONFIG | {"source_url": "https://example.org/other.zim"})
    with pytest.raises(ValueError, match="already installed"):
        zim_jobs.start("ready", CONFIG)


def test_interrupted_job_recovers_paused_without_retry(disk):
    zim_jobs.save(dict(source_id="ready", state="downloading", active=True,
                       configuration=zim_jobs.identity(CONFIG)))
    job = zim_jobs.status("ready")
    assert job["state"] == "paused" and not job["active"]
    assert zim_jobs.read("ready") == job
    with pytest.raises(ValueError, match="retained bytes"):
        zim_jobs.start("ready", CONFIG, resume=True)


def test_pause_retains_partial_and_cancel_removes_it(disk):
    entered = threading.Event()

    def download(sid, config, *, progress, stopped, **kwargs):
        with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
            handle = zim_source.os.open(zim_source.partial_name(sid), zim_source.os.O_CREAT | zim_source.os.O_WRONLY, 0o600, dir_fd=fd)
            with zim_source.os.fdopen(handle, "wb") as output:
                output.write(fixture_bytes()[:80])
        progress(80, len(fixture_bytes()), {"etag": '"v1"'})
        entered.set()
        while not stopped():
            threading.Event().wait(0.01)
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        zim_jobs.start("ready", CONFIG)
        assert entered.wait(5)
        assert zim_jobs.control("ready", CONFIG, "pause")["accepted"]
        join_worker()
    assert zim_jobs.status("ready")["state"] == "paused"
    assert zim_jobs.staged_size("ready") == 80
    assert zim_jobs.control("ready", CONFIG, "cancel")["accepted"]
    assert zim_jobs.staged_size("ready") == 0
    assert zim_jobs.status("ready")["state"] == "cancelled"
    assert list((disk / "library/zim").iterdir()) == []


def test_resume_requires_matching_configuration_and_remote_validator(disk):
    with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
        handle = zim_source.os.open(zim_source.partial_name("ready"), zim_source.os.O_CREAT | zim_source.os.O_WRONLY, 0o600, dir_fd=fd)
        with zim_source.os.fdopen(handle, "wb") as output:
            output.write(fixture_bytes()[:48])
    zim_jobs.save(dict(source_id="ready", state="paused", active=False, configuration=zim_jobs.identity(CONFIG),
                       downloaded_bytes=48, etag='"v1"'))
    with pytest.raises(ValueError, match="matching paused"):
        zim_jobs.start("ready", CONFIG | {"source_url": "https://example.org/different.zim"}, resume=True)
    assert zim_jobs.staged_size("ready") == 48


def test_cancel_does_not_delete_previously_installed_content(disk):
    data = fixture_bytes()
    installed = zim_source.install_stream("previous", CONFIG, BytesIO(data))
    with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
        handle = zim_source.os.open(zim_source.partial_name("ready"), zim_source.os.O_CREAT | zim_source.os.O_WRONLY, 0o600, dir_fd=fd)
        with zim_source.os.fdopen(handle, "wb") as output:
            output.write(data[:40])
    zim_jobs.save(dict(source_id="ready", state="paused", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=40, etag='"v1"'))
    assert zim_jobs.control("ready", CONFIG, "cancel")["accepted"]
    assert (disk / "library/zim" / installed["filename"]).read_bytes() == data


def test_cancel_accepted_during_network_failure_is_not_lost(disk):
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, progress, stopped, **kwargs):
        with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (root, _, _):
            fd = zim_source.os.open(zim_source.partial_name(sid), zim_source.os.O_CREAT | zim_source.os.O_WRONLY,
                                    0o600, dir_fd=root)
            with zim_source.os.fdopen(fd, "wb") as output:
                output.write(fixture_bytes()[:64])
        progress(64, len(fixture_bytes()), {"etag": '"v1"'})
        entered.set()
        assert release.wait(5)
        raise OSError("connection interrupted during cancellation")

    with patch.object(zim_source, "download_resumable", download):
        try:
            zim_jobs.start("ready", CONFIG)
            assert entered.wait(5)
            assert zim_jobs.control("ready", CONFIG, "cancel")["accepted"]
        finally:
            release.set()
        join_worker()
    assert zim_jobs.status("ready")["state"] == "cancelled"
    assert zim_jobs.staged_size("ready") == 0
    assert not list((disk / "library/zim").iterdir())



def test_resume_after_backend_restart_uses_original_etag_and_full_verification(disk, monkeypatch):
    """The persisted job and staged bytes alone must suffice after process exit."""
    from contextlib import contextmanager

    data = fixture_bytes()
    offset = 48
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    partial = stage / ".download-ready.partial"
    partial.write_bytes(data[:offset])
    partial.chmod(0o600)
    zim_jobs.save(dict(source_id="ready", state="downloading", active=True,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=offset,
                       expected_bytes=len(data), etag='"stable"'))
    recovered = zim_jobs.status("ready")
    assert recovered["state"] == "paused" and recovered["downloaded_bytes"] == offset

    class Body(BytesIO):
        total_size = len(data)
        etag = '"stable"'
        last_modified = None

    calls = []

    @contextmanager
    def transport(url, *, offset, validator, **kwargs):
        calls.append((offset, validator))
        yield Body(data[offset:])

    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf", transport)
    zim_jobs.start("ready", CONFIG, resume=True)
    join_worker()
    job = zim_jobs.status("ready")
    assert job["state"] == "completed" and job["verified"]
    assert calls == [(offset, '"stable"')]
    assert zim_jobs.installed(job, CONFIG)["bytes"] == len(data)
    assert not partial.exists()


def test_pausing_or_cancelling_completed_zim_never_deletes_installed_archive(disk):
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    zim_jobs.save(dict(source_id="ready", state="completed", active=False, verified=True,
                       configuration=zim_jobs.identity(CONFIG), artifact=artifact))
    for operation in ("pause", "cancel"):
        with pytest.raises(ValueError):
            zim_jobs.control("ready", CONFIG, operation)
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data


def test_failed_download_retains_bytes_and_records_provenance(disk):
    from contextlib import contextmanager
    data = fixture_bytes()
    class Body(BytesIO):
        total_size = len(data)
        etag = '"edition-1"'
        last_modified = "Thu, 15 Oct 2026 06:29:28 GMT"
    @contextmanager
    def transport(url, **kwargs):
        yield Body(data[:-1] + bytes([data[-1] ^ 1]))
    with patch.object(zim_source.pdf_transport, "open_pdf", transport):
        zim_jobs.start("ready", CONFIG)
        join_worker()
    job = zim_jobs.read("ready")
    assert job["state"] == "failed"
    assert job["retained"] is True
    assert job["downloaded_bytes"] == len(data)
    assert job["verified"] is False
    assert len(job["attempt_history"]) == 1
    attempt = job["attempt_history"][0]
    assert attempt["state"] == "failed"
    assert attempt["retained"] is True
    assert attempt["downloaded_bytes"] == len(data)
    assert attempt["etag"] == '"edition-1"'
    assert attempt["source_url"] == CONFIG["source_url"]
    assert (disk / "staging/zim/.download-ready.partial").stat().st_size == len(data)
    assert not list((disk / "library/zim").iterdir())
    with pytest.raises(ValueError, match="Partial download exists"):
        zim_jobs.start("ready", CONFIG)
    assert len(zim_jobs.read("ready")["attempt_history"]) == 1


def test_attempt_history_is_bounded_and_preserves_outcomes():
    job = {"configuration": {"source_url": CONFIG["source_url"]}, "downloaded_bytes": 0}
    for index in range(20):
        zim_jobs._record_attempt(job, started=True)
        job.update(state="failed", detail="failure", downloaded_bytes=index, retained=True)
        zim_jobs._record_attempt(job)
    assert len(job["attempt_history"]) == zim_jobs.HISTORY_LIMIT
    assert job["attempt_history"][0]["downloaded_bytes"] == 8
    assert job["attempt_history"][-1]["downloaded_bytes"] == 19


def test_invalid_header_after_complete_transfer_is_retained_without_publication(disk):
    from contextlib import contextmanager
    data = bytearray(fixture_bytes())
    data[0] = 0
    class Body(BytesIO):
        total_size = len(data)
        etag = '"stable-header"'
        last_modified = None
    @contextmanager
    def transport(url, **kwargs):
        yield Body(bytes(data))
    with patch.object(zim_source.pdf_transport, "open_pdf", transport):
        zim_jobs.start("ready", CONFIG)
        join_worker()
    job = zim_jobs.read("ready")
    assert job["state"] == "failed" and job["retained"]
    assert "Invalid ZIM header" in job["detail"]
    assert job["attempt_history"][-1]["state"] == "failed"
    assert job["attempt_history"][-1]["expected_bytes"] == len(data)
    assert (disk / "staging/zim/.download-ready.partial").read_bytes() == data
    assert not list((disk / "library/zim").iterdir())


def _retained_fixture(disk, payload, *, expected=None, state="failed"):
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    partial = stage / ".download-ready.partial"
    partial.write_bytes(payload)
    partial.chmod(0o600)
    zim_jobs.save(dict(source_id="ready", state=state, active=False, verified=False,
                       retained=True, configuration=zim_jobs.identity(CONFIG),
                       downloaded_bytes=len(payload),
                       expected_bytes=len(payload) if expected is None else expected,
                       etag='"original-validator"'))
    return partial


def test_revalidate_complete_retained_zim_without_network(disk, monkeypatch):
    data = fixture_bytes()
    partial = _retained_fixture(disk, data)
    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf",
                        lambda *args, **kwargs: pytest.fail("Offline revalidation used the network"))
    assert zim_jobs.revalidate("ready", CONFIG)["accepted"]
    join_worker()
    job = zim_jobs.status("ready")
    assert job["state"] == "completed" and job["verified"] is True
    assert not partial.exists()
    assert zim_jobs.installed(job, CONFIG)["bytes"] == len(data)
    assert job["attempt_history"][-1]["operation"] == "revalidate"
    assert job["attempt_history"][-1]["state"] == "completed"
    with pytest.raises(ValueError, match="No matching retained"):
        zim_jobs.revalidate("ready", CONFIG)


def test_revalidate_invalid_header_preserves_retained_bytes(disk, monkeypatch):
    data = bytearray(fixture_bytes())
    data[0] = 0
    partial = _retained_fixture(disk, bytes(data))
    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf",
                        lambda *args, **kwargs: pytest.fail("Offline revalidation used the network"))
    assert zim_jobs.revalidate("ready", CONFIG)["accepted"]
    join_worker()
    job = zim_jobs.status("ready")
    assert job["state"] == "failed" and not job["verified"] and job["retained"]
    assert "Invalid ZIM header" in job["detail"]
    assert partial.read_bytes() == data
    assert not list((disk / "library/zim").iterdir())


def test_revalidate_refuses_incomplete_or_mismatched_jobs(disk, monkeypatch):
    data = fixture_bytes()
    partial = _retained_fixture(disk, data[:80], expected=len(data))
    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf",
                        lambda *args, **kwargs: pytest.fail("Unexpected network request"))
    with pytest.raises(ValueError, match="complete retained file"):
        zim_jobs.revalidate("ready", CONFIG)
    assert partial.read_bytes() == data[:80]
    with pytest.raises(ValueError, match="matching retained"):
        zim_jobs.revalidate("ready", CONFIG | {"source_url": "https://example.org/another.zim"})
    assert not (disk / "library/zim").exists() or not list((disk / "library/zim").iterdir())


def test_revalidate_checksum_failure_preserves_complete_retained_file(disk):
    data = bytearray(fixture_bytes())
    data[-1] ^= 1
    partial = _retained_fixture(disk, bytes(data))
    zim_jobs.revalidate("ready", CONFIG)
    join_worker()
    job = zim_jobs.read("ready")
    assert job["state"] == "failed" and job["retained"] is True
    assert "checksum mismatch" in job["detail"]
    assert partial.read_bytes() == data
    assert not list((disk / "library/zim").iterdir())


def test_revalidate_recovered_worker_uses_retained_file(disk, monkeypatch):
    data = fixture_bytes()
    partial = _retained_fixture(disk, data, state="paused")
    job = zim_jobs.read("ready")
    job.update(state="verifying", active=True)
    zim_jobs.save(job)
    assert zim_jobs.status("ready")["state"] == "paused"
    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf",
                        lambda *args, **kwargs: pytest.fail("Unexpected download"))
    zim_jobs.revalidate("ready", CONFIG)
    join_worker()
    assert zim_jobs.status("ready")["verified"] is True
    assert not partial.exists()


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_late_verification_progress_preserves_pending_stop_state(disk, action):
    """Worker callbacks must not erase an operator's accepted stop request."""
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, progress, stopped, **kwargs):
        progress(0, len(fixture_bytes()), {"etag": '"stable"'})
        entered.set()
        assert release.wait(5)
        progress(48, len(fixture_bytes()), {"verifying": True})
        assert zim_jobs.read(sid)["state"] == ("pausing" if action == "pause" else "cancelling")
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        try:
            zim_jobs.start("ready", CONFIG)
            assert entered.wait(5)
            assert zim_jobs.control("ready", CONFIG, action)["accepted"]
        finally:
            release.set()
        join_worker()
    assert zim_jobs.status("ready")["state"] == ("paused" if action == "pause" else "cancelled")


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_stop_before_worker_start_remains_durable(disk, monkeypatch, action):
    """An accepted request must survive a worker that has not started yet."""
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, stopped, **kwargs):
        assert stopped()
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        # The real worker can be scheduled after start() returns. Hold its
        # initial lock acquisition while leaving control() free to proceed.
        original_run = zim_jobs._run

        def gated_run(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original_run(*args, **kwargs)

        with patch.object(zim_jobs, "_run", gated_run):
            try:
                zim_jobs.start("ready", CONFIG)
                assert entered.wait(5)
                assert zim_jobs.control("ready", CONFIG, action)["accepted"]
            finally:
                release.set()
            join_worker()
    assert zim_jobs.read("ready")["state"] == ("paused" if action == "pause" else "cancelled")


@pytest.mark.parametrize(("first", "conflict"), [
    ("pause", "cancel"),
    ("cancel", "pause"),
])
def test_conflicting_stop_requests_rejected_without_changing_first_request(disk, first, conflict):
    """A second operator cannot silently reverse a pending stop decision."""
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, stopped, **kwargs):
        entered.set()
        assert release.wait(5)
        assert stopped()
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        try:
            zim_jobs.start("ready", CONFIG)
            assert entered.wait(5)
            accepted = zim_jobs.control("ready", CONFIG, first)
            assert accepted["state"] == ("pausing" if first == "pause" else "cancelling")
            with pytest.raises(ValueError, match="Stop request already pending"):
                zim_jobs.control("ready", CONFIG, conflict)
            assert zim_jobs.read("ready")["state"] == accepted["state"]
        finally:
            release.set()
        join_worker()
    assert zim_jobs.read("ready")["state"] == ("paused" if first == "pause" else "cancelled")


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_repeated_stop_is_idempotent_without_rewriting_pending_state(disk, action):
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, stopped, **kwargs):
        entered.set()
        assert release.wait(5)
        assert stopped()
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        try:
            zim_jobs.start("ready", CONFIG)
            assert entered.wait(5)
            first = zim_jobs.control("ready", CONFIG, action)
            before = zim_jobs.read("ready")
            second = zim_jobs.control("ready", CONFIG, action)
            assert second == first
            assert zim_jobs.read("ready") == before
        finally:
            release.set()
        join_worker()
    assert zim_jobs.read("ready")["state"] == ("paused" if action == "pause" else "cancelled")


@pytest.mark.parametrize("state", ["cancelled", "completed", "failed", "paused"])
def test_stale_partial_is_never_overwritten_by_fresh_start(disk, state):
    """Recovery must not silently truncate bytes after an inconsistent record."""
    payload = fixture_bytes()[:48]
    partial = disk / "staging/zim/.download-ready.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(payload)
    partial.chmod(0o600)
    zim_jobs.save(dict(source_id="ready", state=state, active=False,
                       configuration=zim_jobs.identity(CONFIG),
                       downloaded_bytes=len(payload)))
    error = ("Archive already installed" if state == "completed" else "Partial download exists")
    with pytest.raises(ValueError, match=error):
        zim_jobs.start("ready", CONFIG)
    assert partial.read_bytes() == payload
    assert zim_jobs.read("ready")["state"] == state


@pytest.mark.parametrize("kind", ["empty", "symlink"])
def test_fresh_start_rejects_zero_length_or_unsafe_staged_leftover(disk, kind):
    partial = disk / "staging/zim/.download-ready.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    if kind == "empty":
        partial.touch(mode=0o600)
    else:
        partial.symlink_to(disk / "outside")
    assert zim_jobs.staged_present("ready")
    with pytest.raises(ValueError, match="Partial download exists"):
        zim_jobs.start("ready", CONFIG)
    assert partial.is_symlink() if kind == "symlink" else partial.is_file()


@pytest.mark.parametrize("state", ["paused", "failed", "interrupted"])
def test_cancel_removes_zero_byte_retained_partial_and_allows_restart(disk, state):
    """Explicit cancellation clears an empty staged file, not just nonempty data."""
    with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
        handle = zim_source.os.open(zim_source.partial_name("ready"),
                                    zim_source.os.O_CREAT | zim_source.os.O_EXCL | zim_source.os.O_WRONLY,
                                    0o600, dir_fd=fd)
        zim_source.os.close(handle)
    zim_jobs.save(dict(source_id="ready", state=state, active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    assert zim_jobs.staged_present("ready")
    assert zim_jobs.control("ready", CONFIG, "cancel")["state"] == "cancelled"
    assert not zim_jobs.staged_present("ready")
    assert zim_jobs.read("ready")["state"] == "cancelled"


def test_cancel_keep_retains_partial_and_allows_explicit_resume(disk):
    entered, release = threading.Event(), threading.Event()

    def download(sid, config, *, stopped, **kwargs):
        entered.set()
        assert release.wait(5)
        assert stopped()
        raise zim_source.TransferStopped()

    with patch.object(zim_source, "download_resumable", download):
        try:
            zim_jobs.start("ready", CONFIG)
            assert entered.wait(5)
            assert zim_jobs.control("ready", CONFIG, "cancel-keep")["state"] == "cancelling"
        finally:
            release.set()
        join_worker()
    job = zim_jobs.read("ready")
    assert job["state"] == "paused"
    assert "retained" in job["detail"]
    assert zim_jobs.control("ready", CONFIG, "cancel")["state"] == "cancelled"


def test_cancel_keep_on_failed_job_preserves_staging(disk):
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    result = zim_jobs.control("ready", CONFIG, "cancel-keep")
    assert result["state"] == "paused"
    assert zim_jobs.read("ready")["state"] == "paused"


def test_cancel_keep_requires_exclusive_worker_lock(disk):
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    with patch.object(zim_jobs, "worker_lock", side_effect=BlockingIOError):
        with pytest.raises(BlockingIOError):
            zim_jobs.control("ready", CONFIG, "cancel-keep")
    assert zim_jobs.read("ready")["state"] == "failed"


@pytest.mark.parametrize("state", ["paused", "failed", "interrupted"])
def test_stopped_job_requires_explicit_restart_without_partial(disk, state):
    zim_jobs.save(dict(source_id="ready", state=state, active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    with pytest.raises(ValueError, match="explicit confirmation"):
        zim_jobs.start("ready", CONFIG)
    assert zim_jobs.read("ready")["state"] == state


def test_restart_requires_stopped_job_and_discarded_partial(disk):
    with pytest.raises(ValueError, match="existing stopped"):
        zim_jobs.start("ready", CONFIG, restart=True)
    zim_jobs.save(dict(source_id="ready", state="paused", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    with zim_source.pdf_source._directory("staging", writing=True, format_name="zim") as (fd, _, _):
        handle = zim_source.os.open(zim_source.partial_name("ready"),
                                    zim_source.os.O_CREAT | zim_source.os.O_EXCL | zim_source.os.O_WRONLY,
                                    0o600, dir_fd=fd)
        zim_source.os.close(handle)
    with pytest.raises(ValueError, match="Partial download exists"):
        zim_jobs.start("ready", CONFIG, restart=True)
    assert zim_jobs.staged_present("ready")
    assert zim_jobs.read("ready")["state"] == "paused"


def test_restart_and_resume_are_mutually_exclusive(disk):
    with pytest.raises(ValueError, match="mutually exclusive"):
        zim_jobs.start("ready", CONFIG, restart=True, resume=True)


def test_retry_requires_matching_failed_job_and_has_hard_limit(disk):
    with pytest.raises(ValueError, match="matching failed"):
        zim_jobs.start("ready", CONFIG, retry=True)
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0,
                       attempt_history=[{"operation": "retry"}] * zim_jobs.MAX_RETRIES))
    with pytest.raises(ValueError, match="retry limit"):
        zim_jobs.start("ready", CONFIG, retry=True)
    assert zim_jobs.read("ready")["state"] == "failed"


def test_retry_refuses_unsafe_partial_even_with_failed_job(disk):
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0))
    partial = disk / "staging/zim/.download-ready.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.touch(mode=0o600)
    with pytest.raises(ValueError, match="Partial download exists"):
        zim_jobs.start("ready", CONFIG, retry=True)
    assert partial.exists()


def test_retry_and_resume_cannot_be_combined(disk):
    with pytest.raises(ValueError, match="cannot be combined"):
        zim_jobs.start("ready", CONFIG, retry=True, resume=True)


def test_failed_download_retry_resumes_real_bytes_and_verifies_archive(disk, monkeypatch):
    """Exercise actual HTTP-range transport and promotion, not a mocked worker."""
    from contextlib import contextmanager

    data = fixture_bytes()
    offset = 48
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    partial = stage / ".download-ready.partial"
    partial.write_bytes(data[:offset])
    partial.chmod(0o600)
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=offset,
                       expected_bytes=len(data), etag='"stable"', attempt_history=[]))

    class Body(BytesIO):
        total_size = len(data)
        etag = '"stable"'
        last_modified = None

    calls = []

    @contextmanager
    def transport(url, *, offset, validator, **kwargs):
        calls.append((offset, validator))
        yield Body(data[offset:])

    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf", transport)
    assert zim_jobs.start("ready", CONFIG, retry=True)["accepted"]
    join_worker()
    job = zim_jobs.status("ready")
    assert job["state"] == "completed" and job["verified"]
    assert calls == [(offset, '"stable"')]
    assert job["attempt_history"][-1]["operation"] == "retry"
    assert zim_jobs.installed(job, CONFIG)["bytes"] == len(data)
    assert not partial.exists()


def test_retry_budget_survives_successive_failed_attempts_without_staged_data(disk):
    attempts = []

    def failing(sid, config, **kwargs):
        attempts.append(1)
        raise OSError("offline")

    with patch.object(zim_source, "download_resumable", failing):
        zim_jobs.start("ready", CONFIG)
        join_worker()
        for _ in range(zim_jobs.MAX_RETRIES):
            assert zim_jobs.start("ready", CONFIG, retry=True)["accepted"]
            join_worker()
        with pytest.raises(ValueError, match="retry limit"):
            zim_jobs.start("ready", CONFIG, retry=True)
    job = zim_jobs.read("ready")
    assert job["state"] == "failed"
    assert len(attempts) == zim_jobs.MAX_RETRIES + 1
    assert sum(a.get("operation") == "retry" for a in job["attempt_history"]) == zim_jobs.MAX_RETRIES


@pytest.mark.parametrize("operation", ["pause", "cancel", "cancel-keep"])
def test_publishing_state_rejects_all_stop_controls_without_touching_archive(disk, operation):
    """Atomic promotion cannot be interrupted by a late control request."""
    job = dict(source_id="ready", state="publishing", active=True,
               configuration=zim_jobs.identity(CONFIG), downloaded_bytes=len(fixture_bytes()))
    zim_jobs.save(job)
    before = zim_jobs.read("ready")
    # status() correctly reconciles orphaned 'publishing' records after a
    # restart. Simulate the live worker's state for this control-boundary test.
    with patch.object(zim_jobs, "status", return_value=job):
        with pytest.raises(ValueError, match="already finalizing"):
            zim_jobs.control("ready", CONFIG, operation)
    assert zim_jobs.read("ready") == before
    assert not (disk / "library/zim").exists() or not list((disk / "library/zim").iterdir())


def test_cancelled_job_never_accepts_retry_even_with_old_retry_history(disk):
    zim_jobs.save(dict(source_id="ready", state="cancelled", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0,
                       attempt_history=[{"operation": "retry", "state": "failed"}]))
    with pytest.raises(ValueError, match="matching failed"):
        zim_jobs.start("ready", CONFIG, retry=True)
    assert zim_jobs.read("ready")["state"] == "cancelled"


def test_explicit_restart_resets_exhausted_retry_budget(disk):
    failed = dict(source_id="ready", state="failed", active=False,
                  configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0,
                  attempt_history=[{"operation": "retry"}] * zim_jobs.MAX_RETRIES)
    zim_jobs.save(failed)
    with patch.object(zim_source, "download_resumable", side_effect=OSError("offline")):
        assert zim_jobs.start("ready", CONFIG, restart=True)["accepted"]
        join_worker()
    job = zim_jobs.read("ready")
    assert job["state"] == "failed"
    assert not any(a.get("operation") == "retry" for a in job["attempt_history"])
    with patch.object(zim_source, "download_resumable", side_effect=OSError("offline")):
        assert zim_jobs.start("ready", CONFIG, retry=True)["accepted"]
        join_worker()
    assert sum(a.get("operation") == "retry" for a in zim_jobs.read("ready")["attempt_history"]) == 1


@pytest.mark.parametrize("state", ["queued", "downloading", "verifying", "pausing", "cancelling", "publishing"])
def test_orphaned_active_state_reconciles_without_worker_or_duplicate_download(disk, state):
    """A restart must not leave a phantom active job or spawn another writer."""
    job = dict(source_id="ready", state=state, active=True,
               configuration=zim_jobs.identity(CONFIG), downloaded_bytes=42)
    zim_jobs.save(job)
    recovered = zim_jobs.status("ready")
    assert recovered["state"] == "paused"
    assert recovered["active"] is False
    assert recovered["downloaded_bytes"] == 0
    assert "Backend restarted" in recovered["detail"]
    assert zim_jobs.status("ready") == recovered
    with pytest.raises(ValueError, match="retained bytes"):
        zim_jobs.start("ready", CONFIG, resume=True)
    assert zim_jobs.read("ready") == recovered


def test_cancel_keep_from_failed_state_retains_bytes_and_allows_safe_resume(disk, monkeypatch):
    """An operator may preserve a failed transfer and later resume it explicitly."""
    from contextlib import contextmanager

    data = fixture_bytes()
    stage = disk / "staging/zim"
    stage.mkdir(exist_ok=True)
    partial = stage / ".download-ready.partial"
    partial.write_bytes(data[:48])
    partial.chmod(0o600)
    zim_jobs.save(dict(source_id="ready", state="failed", active=False,
                       configuration=zim_jobs.identity(CONFIG), downloaded_bytes=48,
                       expected_bytes=len(data), etag='"stable"'))
    assert zim_jobs.control("ready", CONFIG, "cancel-keep")["accepted"]
    assert partial.read_bytes() == data[:48]
    assert zim_jobs.status("ready")["state"] == "paused"

    class Body(BytesIO):
        total_size = len(data)
        etag = '"stable"'
        last_modified = None

    calls = []

    @contextmanager
    def transport(url, *, offset, validator, **kwargs):
        calls.append((offset, validator))
        yield Body(data[offset:])

    monkeypatch.setattr(zim_source.pdf_transport, "open_pdf", transport)
    assert zim_jobs.start("ready", CONFIG, resume=True)["accepted"]
    join_worker()
    assert zim_jobs.status("ready")["state"] == "completed"
    assert calls == [(48, '"stable"')]
    assert not partial.exists()
    assert zim_jobs.installed(zim_jobs.status("ready"), CONFIG)["bytes"] == len(data)


def test_retry_budget_is_not_reset_by_truncated_attempt_history(disk):
    """A bounded display history cannot erase the durable retry counter."""
    failed = dict(source_id="ready", state="failed", active=False,
                  configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0,
                  retry_count=zim_jobs.MAX_RETRIES,
                  attempt_history=[{"operation": "resume"}] * zim_jobs.HISTORY_LIMIT)
    zim_jobs.save(failed)
    with pytest.raises(ValueError, match="retry limit"):
        zim_jobs.start("ready", CONFIG, retry=True)
    assert zim_jobs.read("ready")["retry_count"] == zim_jobs.MAX_RETRIES


def test_retry_counter_survives_failed_fresh_retry(disk):
    failed = dict(source_id="ready", state="failed", active=False,
                  configuration=zim_jobs.identity(CONFIG), downloaded_bytes=0,
                  retry_count=1, attempt_history=[])
    zim_jobs.save(failed)
    with patch.object(zim_source, "download_resumable", side_effect=OSError("offline")):
        assert zim_jobs.start("ready", CONFIG, retry=True)["accepted"]
        assert zim_jobs.read("ready")["retry_count"] == 2
        join_worker()
    assert zim_jobs.read("ready")["retry_count"] == 2


def test_changed_candidate_cannot_replace_completed_installed_job(disk):
    """Editing remote metadata must never orphan a published archive."""
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    original = dict(source_id="ready", state="completed", active=False, verified=True,
                    configuration=zim_jobs.identity(CONFIG), artifact=artifact)
    zim_jobs.save(original)
    for change in ({"source_url": "https://example.org/new-edition.zim"},
                   {"sha256": "a" * 64}, {"expected_size_bytes": "999"}):
        with pytest.raises(ValueError, match="replacement is not yet supported"):
            zim_jobs.start("ready", CONFIG | change)
        assert zim_jobs.read("ready") == original
        assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data


def test_completed_job_with_inconsistent_verified_flag_still_blocks_replacement(disk):
    """A malformed completion flag must not authorize an unsafe replacement."""
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    job = dict(source_id="ready", state="completed", active=False, verified=False,
               configuration=zim_jobs.identity(CONFIG), artifact=artifact)
    zim_jobs.save(job)
    with pytest.raises(ValueError, match="replacement is not yet supported"):
        zim_jobs.start("ready", CONFIG | {"source_url": "https://example.org/new.zim"})
    assert zim_jobs.read("ready") == job
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data


def test_replacement_candidate_isolated_from_live_installed_record(disk):
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    live = dict(source_id="ready", state="completed", active=False, verified=True,
                configuration=zim_jobs.identity(CONFIG), artifact=artifact)
    zim_jobs.save(live)
    updated = CONFIG | {"source_url": "https://example.org/edition-2.zim"}
    candidate = zim_jobs.prepare_candidate("ready", updated)
    assert candidate["state"] == "prepared"
    assert candidate["base_artifact"] == artifact
    assert zim_jobs.read_candidate("ready") == candidate
    assert zim_jobs.read("ready") == live
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data
    with pytest.raises(ValueError, match="already exists"):
        zim_jobs.prepare_candidate("ready", updated)
    with pytest.raises(ValueError, match="replacement is not yet supported"):
        zim_jobs.start("ready", updated)
    assert zim_jobs.read("ready") == live


def test_replacement_candidate_requires_verified_installed_archive(disk):
    with pytest.raises(ValueError, match="verified installed archive"):
        zim_jobs.prepare_candidate("ready", CONFIG)
    assert zim_jobs.read_candidate("ready") is None


def test_discard_prepared_candidate_never_deletes_live_archive(disk):
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    live = dict(source_id="ready", state="completed", active=False, verified=True,
                configuration=zim_jobs.identity(CONFIG), artifact=artifact)
    zim_jobs.save(live)
    changed = CONFIG | {"source_url": "https://example.org/next.zim"}
    zim_jobs.prepare_candidate("ready", changed)
    with pytest.raises(ValueError, match="No matching replacement candidate"):
        zim_jobs.discard_candidate("ready", CONFIG)
    assert zim_jobs.read_candidate("ready") is not None
    assert zim_jobs.discard_candidate("ready", changed)["state"] == "discarded"
    assert zim_jobs.read_candidate("ready") is None
    assert zim_jobs.read("ready") == live
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data
    zim_jobs.prepare_candidate("ready", changed)


def test_candidate_discard_refuses_changed_live_archive(disk):
    data = fixture_bytes()
    artifact = zim_source.install_stream("ready", CONFIG, BytesIO(data))
    live = dict(source_id="ready", state="completed", active=False, verified=True,
                configuration=zim_jobs.identity(CONFIG), artifact=artifact)
    zim_jobs.save(live)
    changed = CONFIG | {"source_url": "https://example.org/next.zim"}
    zim_jobs.prepare_candidate("ready", changed)
    zim_jobs.save(live | {"artifact": {"filename": "other.zim"}})
    with pytest.raises(ValueError, match="Installed archive changed"):
        zim_jobs.discard_candidate("ready", changed)
    assert zim_jobs.read_candidate("ready") is not None
    assert (disk / "library/zim" / artifact["filename"]).read_bytes() == data


@pytest.mark.parametrize("payload", [
    "{not-json",
    "[]",
    '{"source_id":"ready","state":"prepared","configuration":{},"base_artifact":{}}',
    '{"source_id":"ready","state":"prepared","configuration":{"format":"zim","source_url":"file:///tmp/a.zim","expected_size_bytes":"","sha256":""},"base_artifact":{}}',
])
def test_invalid_replacement_candidate_metadata_is_rejected(disk, payload):
    path = zim_jobs.candidate_path_for("ready")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    with pytest.raises(ValueError, match="Invalid ZIM candidate"):
        zim_jobs.read_candidate("ready")


def test_replacement_candidate_refuses_symlink_without_following(disk):
    path = zim_jobs.candidate_path_for("ready")
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = path.parent / "unrelated"
    outside.write_text("private content")
    path.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        zim_jobs.read_candidate("ready")
    assert outside.read_text() == "private content"


def test_replacement_candidate_refuses_hardlinked_record(disk):
    path = zim_jobs.candidate_path_for("ready")
    path.parent.mkdir(parents=True, exist_ok=True)
    other = path.parent / "other"
    other.write_text("{}")
    import os
    os.link(other, path)
    with pytest.raises(ValueError, match="Unsafe ZIM candidate"):
        zim_jobs.read_candidate("ready")
    assert other.read_text() == "{}"
