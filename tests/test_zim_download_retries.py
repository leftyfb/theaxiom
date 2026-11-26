"""Fixture-only coverage for automatic retries in both ZIM workers."""
from contextlib import contextmanager
import hashlib
from io import BytesIO
import threading

import pytest

from theaxiom import pdf_transport, zim_jobs, zim_source
from test_zim_source import CONFIG, fixture_bytes
from test_zim_source import disk as disk


def join_worker():
    for thread in threading.enumerate():
        if thread.name in {"zim-acquisition", "zim-replacement"}:
            thread.join(5)
            assert not thread.is_alive()


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("etag", [None, '"stable"'])
def test_retry_resumes_or_restarts_matching_partial(disk, monkeypatch, replacement, etag):
    sid = "ready"
    data = fixture_bytes()
    config = CONFIG
    installed = None
    if replacement:
        old_body = data[:-17] + b"x"
        old_data = old_body + hashlib.md5(old_body, usedforsecurity=False).digest()
        artifact = zim_source.install_stream(sid, CONFIG, BytesIO(old_data))
        live = dict(source_id=sid, state="completed", active=False, verified=True,
                    configuration=zim_jobs.identity(CONFIG), artifact=artifact)
        zim_jobs.save(live)
        installed = (live, disk / "library/zim" / artifact["filename"], old_data)
        config = CONFIG | {"source_url": "https://example.org/next.zim"}
        zim_jobs.prepare_candidate(sid, config)
    calls = []

    @contextmanager
    def open_fixture(url, *, offset, validator, **kwargs):
        calls.append((offset, validator))

        class Stream(BytesIO):
            last_modified = None
            total_size = len(data)

            def __init__(self, payload):
                super().__init__(payload)
                self.etag = etag

            def read(self, size=-1, /):
                if len(calls) == 1 and self.tell() >= 32:
                    raise pdf_transport.RetryableDownloadError("fixture disconnect")
                return super().read(size)

        with Stream(data[offset:]) as stream:
            yield stream

    monkeypatch.setattr(pdf_transport, "open_pdf", open_fixture)
    monkeypatch.setattr(pdf_transport, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(zim_source, "CHUNK", 32)
    if replacement:
        zim_jobs.start_candidate_acquisition(sid, config)
        join_worker()
        result = zim_jobs.read_candidate(sid)
        assert result and result["state"] == "verified", result
        assert installed is not None
        assert zim_jobs.read(sid) == installed[0]
        assert installed[1].read_bytes() == installed[2]
    else:
        zim_jobs.start(sid, config)
        join_worker()
        result = zim_jobs.read(sid)
        assert result and result["state"] == "completed", result
        assert len(result["attempt_history"]) == 1
    assert calls == [(0, None), (32, etag) if etag else (0, None)]
    assert result["automatic_retry_count"] == 1


def test_backoff_can_be_cancelled(disk, monkeypatch):
    entered = threading.Event()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise pdf_transport.RetryableDownloadError("fixture disconnect")

    original = pdf_transport.run_with_download_retries

    def run(attempt, *, wait, **kwargs):
        def wait_for_cancel(delay):
            entered.set()
            return wait(delay)
        return original(attempt, wait=wait_for_cancel, **kwargs)

    monkeypatch.setattr(pdf_transport, "open_pdf", fail)
    monkeypatch.setattr(pdf_transport, "run_with_download_retries", run)
    monkeypatch.setattr(pdf_transport, "RETRY_DELAYS", (10, 10, 10))
    zim_jobs.start("ready", CONFIG)
    assert entered.wait(5)
    job = zim_jobs.status("ready")
    assert job and job["state"] == "retrying"
    zim_jobs.control("ready", CONFIG, "cancel")
    join_worker()
    job = zim_jobs.read("ready")
    assert job and job["state"] == "cancelled"
    assert calls == [1]


@pytest.mark.parametrize("transient", [False, True])
def test_final_error_and_manual_retry_after_exhaustion(disk, monkeypatch, transient):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        error = pdf_transport.RetryableDownloadError if transient else ValueError
        raise error(f"fixture failure {len(calls)}")

    monkeypatch.setattr(pdf_transport, "open_pdf", fail)
    monkeypatch.setattr(pdf_transport, "RETRY_DELAYS", (0, 0, 0))
    zim_jobs.start("ready", CONFIG)
    join_worker()
    job = zim_jobs.read("ready")
    assert job and job["state"] == "failed"
    assert job["automatic_retry_count"] == (3 if transient else 0)
    assert job["detail"] == f"fixture failure {4 if transient else 1}"
    assert job.get("retry_count", 0) == 0
    if transient:
        assert zim_jobs.start("ready", CONFIG, retry=True)["accepted"]
        join_worker()
        job = zim_jobs.read("ready")
        assert job and job["retry_count"] == 1
        assert len(calls) == 8
