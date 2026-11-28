"""Deterministic tests for metadata-only educational channel scope planning."""
import sqlite3

import pytest

from theaxiom import kolibri_scope as scope

CID = "c" * 32
ROOT = "a" * 32
MATH = "b" * 32
LESSON = "d" * 32
SCIENCE = "e" * 32
OTHER = "f" * 32
VIDEO = "1" * 32
CAPTION = "2" * 32


@pytest.fixture
def channel(tmp_path):
    path = tmp_path / "channel.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE content_channelmetadata (id, name, version, root_id);
            CREATE TABLE content_contentnode
                (id, channel_id, parent_id, title, kind, tree_id, lft, rght, license_name);
            CREATE TABLE content_file (contentnode_id, local_file_id);
            CREATE TABLE content_localfile (id, extension, file_size_bigint);
        """)
        conn.execute("INSERT INTO content_channelmetadata VALUES (?, ?, ?, ?)", (CID, "Fixture", 3, ROOT))
        conn.executemany("INSERT INTO content_contentnode VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (ROOT, CID, None, "Root", "topic", 1, 1, 10, None),
            (MATH, CID, ROOT, "Math", "topic", 1, 2, 5, None),
            (LESSON, CID, MATH, "Fractions", "video", 1, 3, 4, "CC BY"),
            (SCIENCE, CID, ROOT, "Science", "topic", 1, 6, 9, None),
            (OTHER, CID, SCIENCE, "Physics", "video", 1, 7, 8, None),
        ])
        conn.executemany("INSERT INTO content_file VALUES (?, ?)", [
            (LESSON, VIDEO), (LESSON, CAPTION), (OTHER, VIDEO),
        ])
        conn.executemany("INSERT INTO content_localfile VALUES (?, ?, ?)", [
            (VIDEO, "mp4", 500), (CAPTION, "vtt", 40),
        ])
    return path


def test_channel_and_browse(channel):
    assert scope.inspect_channel(channel, CID).version == 3
    assert [n.title for n in scope.children(channel, CID)] == ["Math", "Science"]
    assert [n.title for n in scope.children(channel, CID, MATH)] == ["Fractions"]


def test_explicit_scope_deduplicates_overlap_and_files(channel):
    before = channel.read_bytes()
    plan = scope.plan_scope(channel, CID, [MATH, LESSON, SCIENCE])
    assert plan.resource_count == 2
    assert plan.total_bytes == 540
    assert plan.licenses == ("CC BY",)
    assert plan.missing_license_count == 1
    assert plan.files[0].storage_path == f"1/1/{VIDEO}.mp4"
    assert channel.read_bytes() == before


def test_one_lesson(channel):
    plan = scope.plan_scope(channel, CID, [LESSON])
    assert plan.resource_count == 1
    assert plan.total_bytes == 540
    assert plan.missing_license_count == 0


@pytest.mark.parametrize("selection", [[], [ROOT], ["../foo"], ["0" * 32], [MATH] * 65])
def test_scope_fails_closed(channel, selection):
    with pytest.raises(scope.ScopeError):
        scope.plan_scope(channel, CID, selection)


def test_full_scope_requires_confirmation(channel):
    assert scope.plan_scope(channel, CID, [ROOT], allow_full=True).resource_count == 2


def test_wrong_channel_and_missing_size(channel):
    with pytest.raises(scope.ScopeError):
        scope.inspect_channel(channel, "0" * 32)
    with sqlite3.connect(channel) as conn:
        conn.execute("UPDATE content_localfile SET file_size_bigint=NULL")
    with pytest.raises(scope.ScopeError):
        scope.plan_scope(channel, CID, [MATH])


def test_legacy_file_size_column(channel):
    with sqlite3.connect(channel) as conn:
        conn.execute("ALTER TABLE content_localfile RENAME COLUMN file_size_bigint TO file_size")
    assert scope.plan_scope(channel, CID, [MATH]).total_bytes == 540


def test_symlink_and_invalid_database(channel, tmp_path):
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(channel)
    with pytest.raises(scope.ScopeError):
        scope.inspect_channel(link, CID)
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises(scope.ScopeError):
        scope.inspect_channel(invalid, CID)
