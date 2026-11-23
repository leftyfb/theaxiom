"""Synthetic fail-closed tests for legacy ZIM adoption."""
import os

import pytest

from theaxiom import admin_api, storage_settings, zim_adoption, zim_catalog, zim_jobs


@pytest.fixture
def adoption_disk(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(root), storage_mount=str(root), storage_source="fixture"))
    from contextlib import contextmanager

    @contextmanager
    def directory(*args, **kwargs):
        fd = os.open(root / "library" / "zim", os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield fd, zim_adoption.pdf_source._mount_id(fd), 0
        finally:
            os.close(fd)

    monkeypatch.setattr(zim_adoption.pdf_source, "_directory", directory)
    monkeypatch.setattr(zim_adoption, "PINNED_FILENAME", "wikipedia_en_all_maxi_test.zim")
    monkeypatch.setattr(zim_adoption, "PINNED_BYTES", 128)
    monkeypatch.setattr(zim_adoption, "PINNED_SHA256", "a" * 64)
    legacy_dir = root / "library" / "zim"
    legacy_dir.mkdir(parents=True)
    payload = b"ZIM\x04" + bytes(4) + b"a" * 16 + bytes(104)
    archive = legacy_dir / zim_adoption.PINNED_FILENAME
    archive.write_bytes(payload)
    archive.chmod(0o600)
    unit = {"InvocationID": "a" * 32, "ActiveState": "inactive",
            "Result": "success", "ExecMainStatus": "0"}
    evidence = {"timestamp": 1234.5, "timestamp_ns": 1234500000000,
                "marker": "synthetic native evidence", "invocation_id": "a" * 32}
    monkeypatch.setattr(zim_adoption.acquisition_monitor, "unit_state", lambda: unit)
    monkeypatch.setattr(zim_adoption.acquisition_monitor,
                        "pinned_invocation_evidence",
                        lambda entry, path, final_stat, current_unit: evidence)
    return tmp_path, root, archive, payload


def adopt_fixture(disk):
    _, _, archive, _ = disk
    return zim_adoption.adopt(archive, dict(
        format="zim", source_url=zim_adoption.PINNED_SOURCE_URL,
        expected_size_bytes=str(zim_adoption.PINNED_BYTES), sha256=zim_adoption.PINNED_SHA256))


def test_adopt_and_rollback_in_place_on_shared_parent(adoption_disk):
    _, _, archive, payload = adoption_disk
    archive.parent.chmod(0o777)
    before = zim_jobs.archive_identity(archive.stat())
    record = adopt_fixture(adoption_disk)
    assert adopt_fixture(adoption_disk) == record
    assert zim_adoption.managed()
    job = zim_jobs.read("wikipedia-en")
    assert zim_jobs.installed(job, job["configuration"]) == job["artifact"]
    assert job["artifact"]["filename"] == archive.name
    assert admin_api.select_lifecycle("wikipedia-en", job["configuration"])["zim_download"]
    assert zim_adoption.rollback()["state"] == "rolled_back"
    assert zim_adoption.rollback()["state"] == "rolled_back"
    assert zim_jobs.read("wikipedia-en") is None
    assert not zim_adoption.managed()
    assert archive.read_bytes() == payload
    assert zim_jobs.archive_identity(archive.stat()) == before
    assert set(archive.parent.iterdir()) == {archive}


def test_same_size_replacement_does_not_inherit_evidence(adoption_disk):
    _, _, archive, payload = adoption_disk
    adopt_fixture(adoption_disk)
    replacement = archive.with_suffix(".other")
    replacement.write_bytes(payload)
    replacement.chmod(0o600)
    replacement.replace(archive)
    job = zim_jobs.read("wikipedia-en")
    assert zim_jobs.installed(job, job["configuration"]) is None
    with pytest.raises(zim_adoption.AdoptionError, match="identity changed"):
        adopt_fixture(adoption_disk)


def test_adoption_resumes_after_ledger_write(adoption_disk, monkeypatch):
    original = zim_adoption._json_atomic

    def crash_on_completion(path, value):
        if value["state"] == "completed":
            raise OSError("interrupted")
        original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(zim_adoption, "_json_atomic", crash_on_completion)
        with pytest.raises(OSError, match="interrupted"):
            adopt_fixture(adoption_disk)
    assert adopt_fixture(adoption_disk)["state"] == "completed"


def test_missing_current_evidence_refuses_adoption(adoption_disk, monkeypatch):
    monkeypatch.setattr(zim_adoption.acquisition_monitor, "pinned_invocation_evidence", lambda *args: None)
    with pytest.raises(zim_adoption.AdoptionError, match="evidence is unavailable"):
        adopt_fixture(adoption_disk)
    assert zim_jobs.read("wikipedia-en") is None


def test_managed_transition_survives_removal_of_installed_job(adoption_disk):
    adopt_fixture(adoption_disk)
    zim_jobs.path_for("wikipedia-en").unlink()
    assert zim_adoption.managed()
    with pytest.raises(zim_adoption.AdoptionError, match="acquisition state changed"):
        zim_adoption.rollback()


def test_shared_manifest_keeps_existing_book_id_once(adoption_disk):
    _, _, archive, payload = adoption_disk
    adopted = adopt_fixture(adoption_disk)["job"]
    another_url = "https://download.kiwix.org/zim/other/another.zim"
    digest = "b" * 64
    another_name = f"another.{digest}.zim"
    another = archive.with_name(another_name)
    another.write_bytes(payload[:8] + b"b" * 16 + payload[24:])
    another.chmod(0o600)
    config = dict(format="zim", source_url=another_url, expected_size_bytes="128", sha256=digest)
    zim_jobs.save(dict(source_id="another", state="completed", active=False, verified=True,
                       configuration=config, artifact=dict(filename=another_name, source_url=another_url,
                                                           sha256=digest, bytes=128)))

    records = [
        {"source_id": sid, "configuration": settings, "profile": {"title": sid, "language": "English"}}
        for sid, settings in [("wikipedia-en", adopted["configuration"]), ("another", config)]
    ]
    books = zim_catalog.verified_books(records, enabled={"wikipedia-en": True, "another": True})
    assert [zim_catalog.content_name(book) for book in books] == [archive.stem, another.stem]
