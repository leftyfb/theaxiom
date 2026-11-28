import sqlite3

import pytest

from theaxiom import kolibri_jobs, kolibri_native
from theaxiom.kolibri_publication import Registry

CID = "a"*32
NODE = "b"*32


def test_native_job_lifecycle(tmp_path, monkeypatch):
    database = tmp_path/"db.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE content_channelmetadata (id, version)")
    registry = Registry(tmp_path/"registry", database)
    packages = tmp_path/"packages"
    versions = {}

    def acquire(root, selection, **kwargs):
        root.mkdir()
        versions[str(root)] = selection.version
        kwargs["progress"](selection.media_bytes, selection.media_bytes)
        return str(selection.version)*64

    monkeypatch.setattr(kolibri_native, "acquire", acquire)
    monkeypatch.setattr(kolibri_native, "verify", lambda *args: True)

    def runner(command, **kwargs):
        assert registry.pending.exists()
        with sqlite3.connect(database) as conn:
            if command[2] == "importchannel":
                conn.execute("INSERT INTO content_channelmetadata VALUES (?,?)",
                             (CID, versions[command[-1]]))
            elif command[2] == "deletechannel":
                conn.execute("DELETE FROM content_channelmetadata WHERE id=?", (CID,))
    jobs = kolibri_jobs.Jobs(tmp_path/"jobs", packages, registry, "/bin/kolibri",
                             tmp_path/"home", runner=runner)
    def selection(version, nodes=(NODE,)):
        return kolibri_native.Selection(CID, nodes, version, "f"*64, 100)
    jobs.download("school", selection(1))
    assert not registry.acknowledged()
    jobs.publish("school", True)
    assert registry.acknowledged()["school"]["version"] == 1
    with pytest.raises(ValueError):
        jobs.download("school", selection(2, ("c"*32,)), update=True)
    jobs.download("school", selection(2), update=True)
    assert registry.acknowledged()["school"]["version"] == 1
    jobs.switch("school")
    assert registry.acknowledged()["school"]["version"] == 2
    jobs.switch("school", rollback=True)
    assert registry.acknowledged()["school"]["version"] == 1
    jobs.delete("school", staged=True)
    with pytest.raises(ValueError):
        jobs.delete("school")
    jobs.publish("school", False)
    assert not registry.acknowledged()
    jobs.delete("school")
    assert not jobs.read("school").get("installed")
