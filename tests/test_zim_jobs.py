"""Regression tests for durable ZIM lifecycle and safe cross-process serialization."""
from io import BytesIO
import threading
from unittest.mock import patch

import pytest

from theaxiom import zim_jobs, zim_source
from test_zim_source import CONFIG, disk, fixture_bytes


def join_worker():
    for thread in threading.enumerate():
        if thread.name == "zim-acquisition":
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
        assert threading.Event().wait(0.01) is None
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
