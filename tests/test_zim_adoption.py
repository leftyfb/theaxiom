"""Synthetic fail-closed tests for legacy ZIM adoption."""
import json
import os
from pathlib import Path

import pytest

from theaxiom import zim_adoption, zim_jobs, storage_settings


@pytest.fixture
def adoption_disk(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    storage_settings.save_settings(dict(storage_settings.DEFAULT_SETTINGS, storage_root=str(root)))
    monkeypatch.setattr(zim_adoption, "PINNED_FILENAME", "wikipedia_en_all_maxi_test.zim")
    monkeypatch.setattr(zim_adoption, "PINNED_BYTES", 128)
    monkeypatch.setattr(zim_adoption, "PINNED_SHA256", "a" * 64)
    legacy_dir = root / "library" / "zim"
    legacy_dir.mkdir(parents=True)
    payload = b"ZIM synthetic archive\0" + bytes(106)
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


def test_adopt_moves_exact_inode_and_writes_ordinary_completed_job(adoption_disk):
    tmp_path, root, archive, payload = adoption_disk
    before = archive.stat()
    result = zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"},
                                tmp_path / "adoption.json")
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    assert result["state"] == "completed"
    assert result["evidence"]["invocation_id"] == "a" * 32
    assert result["evidence"]["marker"] == "synthetic native evidence"
    assert not archive.exists() and destination.read_bytes() == payload
    after = destination.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    job = zim_jobs.read("wikipedia-en")
    assert job["state"] == "completed" and job["verified"] is True
    assert job["adopted_from_legacy"]["path"] == str(archive)
    assert job["artifact"]["filename"] == destination.name


def test_adopted_job_is_visible_to_normal_installed_ledger_check(adoption_disk):
    tmp_path, _, archive, _ = adoption_disk
    config = {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}
    zim_adoption.adopt(archive, config, tmp_path / "adoption.json")
    job = zim_jobs.read("wikipedia-en")
    assert zim_jobs.installed(job, job["configuration"]) == job["artifact"]
    mismatched = dict(job["configuration"], source_url="https://download.kiwix.org/zim/wikipedia/other.zim")
    assert zim_jobs.installed(job, mismatched) is None


def test_adopt_is_idempotent_only_for_same_durable_transition(adoption_disk):
    tmp_path, _, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    first = zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"}, journal)
    second = zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"}, journal)
    assert second == first
    with pytest.raises(zim_adoption.AdoptionError, match="exact pinned source URL"):
        zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://other.test/wiki.zim"}, journal)


def test_adopt_rejects_missing_native_evidence(adoption_disk, monkeypatch):
    tmp_path, _, archive, _ = adoption_disk
    monkeypatch.setattr(zim_adoption.acquisition_monitor,
                        "pinned_invocation_evidence",
                        lambda entry, path, final_stat, unit: None)
    with pytest.raises(zim_adoption.AdoptionError,
                       match="current acquisition evidence is unavailable"):
        zim_adoption.adopt(archive,
                           {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                           tmp_path / "adoption.json")
    assert archive.exists()


def test_adopt_rejects_existing_destination(adoption_disk):
    tmp_path, root, archive, _ = adoption_disk
    (root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")).write_bytes(b"occupied")
    with pytest.raises(zim_adoption.AdoptionError, match="already exists"):
        zim_adoption.adopt(archive,
                           {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                           tmp_path / "adoption.json")
    assert archive.exists()


def test_adopt_rejects_unsafe_archive_and_rollback_restores_inode(adoption_disk):
    tmp_path, _, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    result = zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"}, journal)
    destination = archive.parent / result["destination"]
    destination.write_bytes(b"tampered") if False else None
    assert zim_adoption.rollback(journal)["state"] == "rolled_back"
    assert archive.exists() and not destination.exists()
    assert zim_jobs.read("wikipedia-en") is None


def test_rollback_fails_closed_when_adopted_inode_changes(adoption_disk):
    tmp_path, _, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    result = zim_adoption.adopt(archive, {"format": "zim", "source_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim"}, journal)
    destination = archive.parent / result["destination"]
    replacement = destination.with_suffix(".replacement")
    destination.rename(replacement)
    destination.write_bytes(b"wrong")
    with pytest.raises(zim_adoption.AdoptionError, match="inode changed"):
        zim_adoption.rollback(journal)
    assert replacement.exists() and destination.exists()


def test_adopt_recovers_crash_after_link_before_unlink(adoption_disk, monkeypatch):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    original_move = zim_adoption._move_noreplace

    def link_then_crash(parent_fd, source, target):
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                 follow_symlinks=False)
        raise RuntimeError("crash after link")

    monkeypatch.setattr(zim_adoption, "_move_noreplace", link_then_crash)
    with pytest.raises(RuntimeError, match="crash after link"):
        zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    assert archive.exists() and destination.exists()
    assert archive.stat().st_ino == destination.stat().st_ino

    monkeypatch.setattr(zim_adoption, "_move_noreplace", original_move)
    result = zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    assert result["state"] == "completed"
    assert not archive.exists() and destination.exists()


def test_adopt_recovers_crash_after_unlink_before_moved_journal(adoption_disk, monkeypatch):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    original_atomic = zim_adoption._json_atomic
    calls = {"moved": 0}

    def crash_on_moved(path, value):
        if value.get("state") == "moved" and not calls["moved"]:
            calls["moved"] += 1
            raise RuntimeError("crash after unlink")
        return original_atomic(path, value)

    monkeypatch.setattr(zim_adoption, "_json_atomic", crash_on_moved)
    with pytest.raises(RuntimeError, match="crash after unlink"):
        zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    assert not archive.exists() and destination.exists()
    monkeypatch.setattr(zim_adoption, "_json_atomic", original_atomic)
    assert zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)["state"] == "completed"


def test_prepared_recovery_rejects_mtime_change(adoption_disk, monkeypatch):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    original_move = zim_adoption._move_noreplace

    def link_then_crash(parent_fd, source, target):
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                 follow_symlinks=False)
        raise RuntimeError("crash after link")

    monkeypatch.setattr(zim_adoption, "_move_noreplace", link_then_crash)
    with pytest.raises(RuntimeError):
        zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    os.utime(destination, ns=(destination.stat().st_atime_ns, destination.stat().st_mtime_ns + 1))
    monkeypatch.setattr(zim_adoption, "_move_noreplace", original_move)
    with pytest.raises(zim_adoption.AdoptionError, match="inode changed"):
        zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)


def test_rollback_recovers_both_links_after_movement_crash(adoption_disk, monkeypatch):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    original_move = zim_adoption._move_noreplace

    def link_then_crash(parent_fd, source, target):
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                 follow_symlinks=False)
        raise RuntimeError("crash during rollback move")

    monkeypatch.setattr(zim_adoption, "_move_noreplace", link_then_crash)
    with pytest.raises(RuntimeError):
        zim_adoption.rollback(journal)
    assert json.loads(journal.read_text())["state"] == "rolling_back"
    assert archive.exists() and destination.exists()
    monkeypatch.setattr(zim_adoption, "_move_noreplace", original_move)
    assert zim_adoption.rollback(journal)["state"] == "rolled_back"
    assert archive.exists() and not destination.exists()


def test_rollback_resumes_from_legacy_only(adoption_disk):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    os.link(destination, archive, follow_symlinks=False)
    destination.unlink()
    record = json.loads(journal.read_text())
    record["state"] = "rolling_back"
    zim_adoption._json_atomic(journal, record)
    assert zim_adoption.rollback(journal)["state"] == "rolled_back"
    assert archive.exists() and not destination.exists() and zim_jobs.read("wikipedia-en") is None


def test_rollback_resumes_when_ledger_was_already_deleted(adoption_disk):
    tmp_path, root, archive, _ = adoption_disk
    journal = tmp_path / "adoption.json"
    zim_adoption.adopt(archive, {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL}, journal)
    destination = root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")
    os.link(destination, archive, follow_symlinks=False)
    destination.unlink()
    zim_jobs.path_for("wikipedia-en").unlink()
    record = json.loads(journal.read_text())
    record["state"] = "rolling_back"
    zim_adoption._json_atomic(journal, record)
    assert zim_adoption.rollback(journal)["state"] == "rolled_back"
    assert archive.exists() and not destination.exists()


def _managed_destination(root):
    return root / "library" / "zim" / ("wikipedia-en." + "a" * 64 + ".zim")


def test_adopt_refuses_world_writable_archive_parent_without_mutation(adoption_disk):
    tmp_path, root, archive, payload = adoption_disk
    parent = archive.parent
    destination = _managed_destination(root)
    journal = tmp_path / "adoption.json"
    destination.write_bytes(b"pre-existing managed bytes")
    before_archive = archive.stat()
    before_destination = destination.stat()

    parent.chmod(0o777)
    try:
        with pytest.raises(zim_adoption.AdoptionError, match="writable|unsafe"):
            zim_adoption.adopt(archive,
                               {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                               journal)
    finally:
        parent.chmod(0o755)

    assert archive.read_bytes() == payload
    after_archive = archive.stat()
    assert (after_archive.st_dev, after_archive.st_ino,
            after_archive.st_size, after_archive.st_mtime_ns) == (
                before_archive.st_dev, before_archive.st_ino,
                before_archive.st_size, before_archive.st_mtime_ns)
    assert destination.read_bytes() == b"pre-existing managed bytes"
    after_destination = destination.stat()
    assert (after_destination.st_dev, after_destination.st_ino,
            after_destination.st_size, after_destination.st_mtime_ns) == (
                before_destination.st_dev, before_destination.st_ino,
                before_destination.st_size, before_destination.st_mtime_ns)
    assert not journal.exists()
    assert zim_jobs.read("wikipedia-en") is None


def test_unsafe_parent_preserves_substituted_destination_and_prepared_journal(
        adoption_disk, monkeypatch):
    tmp_path, root, archive, payload = adoption_disk
    parent = archive.parent
    destination = _managed_destination(root)
    journal = tmp_path / "adoption.json"
    original_move = zim_adoption._move_noreplace

    def link_then_crash(parent_fd, source, target):
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
        raise RuntimeError("synthetic interrupted adoption")

    monkeypatch.setattr(zim_adoption, "_move_noreplace", link_then_crash)
    with pytest.raises(RuntimeError, match="synthetic interrupted adoption"):
        zim_adoption.adopt(archive,
                           {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                           journal)
    assert archive.exists() and destination.exists()
    prepared = json.loads(journal.read_text())
    assert prepared["state"] == "prepared"

    destination.unlink()
    destination.write_bytes(b"substituted destination")
    before_archive = archive.stat()
    before_substitute = destination.stat()
    before_journal = journal.read_bytes()
    monkeypatch.setattr(zim_adoption, "_move_noreplace", original_move)

    parent.chmod(0o777)
    try:
        with pytest.raises(zim_adoption.AdoptionError, match="writable|unsafe"):
            zim_adoption.adopt(archive,
                               {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                               journal)
    finally:
        parent.chmod(0o755)

    assert archive.read_bytes() == payload
    after_archive = archive.stat()
    assert (after_archive.st_dev, after_archive.st_ino,
            after_archive.st_mtime_ns) == (
                before_archive.st_dev, before_archive.st_ino,
                before_archive.st_mtime_ns)
    assert destination.read_bytes() == b"substituted destination"
    after_substitute = destination.stat()
    assert (after_substitute.st_dev, after_substitute.st_ino,
            after_substitute.st_mtime_ns) == (
                before_substitute.st_dev, before_substitute.st_ino,
                before_substitute.st_mtime_ns)
    assert journal.read_bytes() == before_journal


def test_interrupted_adoption_recovers_after_parent_safe_again(adoption_disk, monkeypatch):
    tmp_path, root, archive, _ = adoption_disk
    parent = archive.parent
    destination = _managed_destination(root)
    journal = tmp_path / "adoption.json"
    original_move = zim_adoption._move_noreplace

    def link_then_crash(parent_fd, source, target):
        os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
        raise RuntimeError("synthetic interrupted adoption")

    monkeypatch.setattr(zim_adoption, "_move_noreplace", link_then_crash)
    with pytest.raises(RuntimeError, match="synthetic interrupted adoption"):
        zim_adoption.adopt(archive,
                           {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                           journal)
    monkeypatch.setattr(zim_adoption, "_move_noreplace", original_move)
    before_journal = journal.read_bytes()

    parent.chmod(0o777)
    try:
        with pytest.raises(zim_adoption.AdoptionError, match="writable|unsafe"):
            zim_adoption.adopt(archive,
                               {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                               journal)
        assert archive.exists() and destination.exists()
        assert journal.read_bytes() == before_journal
    finally:
        parent.chmod(0o755)

    result = zim_adoption.adopt(archive,
                                {"format": "zim", "source_url": zim_adoption.PINNED_SOURCE_URL},
                                journal)
    assert result["state"] == "completed"
    assert not archive.exists() and destination.exists()
    assert destination.stat().st_ino == result["identity"]["inode"]
