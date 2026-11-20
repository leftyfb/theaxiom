import threading
from unittest.mock import patch

import pytest

from theaxiom import storage_settings, zim_jobs, zim_source
from test_zim_source import CONFIG, disk, fixture_bytes
from io import BytesIO


def test_job_records_url_only_and_rejects_concurrent_start(disk):
    entered, release = threading.Event(), threading.Event()
    def download(sid, config, progress):
        entered.set()
        assert release.wait(5)
        return zim_source.install_stream(sid, config, BytesIO(fixture_bytes()), progress=progress)
    with patch.object(zim_source, "download", download):
        try:
            assert zim_jobs.start("ready", CONFIG)["accepted"]
            assert entered.wait(5)
            job = zim_jobs.status("ready")
            assert job["active"] and job["configuration"]["sha256"] == ""
            with pytest.raises(BlockingIOError):
                zim_jobs.start("other", CONFIG)
        finally:
            release.set()
        for thread in threading.enumerate():
            if thread.name == "zim-acquisition":
                thread.join(5)
    job = zim_jobs.status("ready")
    assert job["state"] == "completed"
    assert zim_jobs.installed(job, CONFIG)["bytes"] == len(fixture_bytes())
    assert not zim_jobs.installed(job, CONFIG | {"source_url": "https://example.org/other.zim"})
    with pytest.raises(ValueError, match="already installed"):
        zim_jobs.start("ready", CONFIG)


def test_interrupted_job_recovered_without_retry(disk):
    zim_jobs.save(dict(source_id="ready", state="downloading", active=True,
                       configuration=zim_jobs.identity(CONFIG)))
    job = zim_jobs.status("ready")
    assert job["state"] == "interrupted" and not job["active"]
    assert zim_jobs.read("ready") == job
