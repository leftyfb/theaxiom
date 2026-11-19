"""Read-only adapter for the standalone Wikipedia acquisition service."""
from __future__ import annotations

import copy
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

from . import storage, storage_settings

UNIT = "acquire-wikipedia-zim.service"
INTERVAL = 2
STALE_AFTER = 10


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True,
                          timeout=3).stdout


def unit_state():
    fields = "LoadState,ActiveState,SubState,Result,ExecMainStatus,InvocationID,ControlGroup"
    return dict(line.split("=", 1) for line in command(
        "systemctl", "show", UNIT, "--property=" + fields
    ).splitlines() if "=" in line)


def process_ids(unit):
    group = unit.get("ControlGroup", "")
    if not group.startswith("/") or ".." in Path(group).parts:
        return set()
    try:
        pids = (Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs").read_text().split()
    except OSError:
        return set()
    return {pid for pid in pids if pid.isdecimal()}


def processes(unit):
    names = set()
    for pid in process_ids(unit):
        if pid.isdecimal():
            try:
                names.add((Path("/proc") / pid / "comm").read_text().strip())
            except OSError:
                pass
    return names


def checksum_progress(unit, candidates, expected_bytes, proc_root=Path("/proc")):
    """Estimate bytes read from a live hash descriptor, never read ZIM data.

    Candidates are sampled regular-file metadata from the known acquisition
    paths. Match device/inode and size, not just the descriptor's pathname.
    A process may exit or change descriptor during sampling: fail unknown.
    """
    matches = []
    for pid in process_ids(unit):
        proc = proc_root / pid
        try:
            if (proc / "comm").read_text().strip() != "sha256sum":
                continue
            for fd in (proc / "fd").iterdir():
                if not fd.name.isdecimal():
                    continue
                try:
                    before = fd.stat()
                    target = os.readlink(fd)
                    # The script opens paths via /proc/self/fd; the kernel
                    # resolves those to the actual filesystem pathname.
                    for path, info, pass_number, passes in candidates:
                        if (info is None or target != str(path) or
                            not stat.S_ISREG(before.st_mode) or
                            (before.st_dev, before.st_ino, before.st_size) !=
                            (info.st_dev, info.st_ino, expected_bytes)):
                            continue
                        lines = (proc / "fdinfo" / fd.name).read_text().splitlines()
                        position = int(next(line.split()[1] for line in lines
                                            if line.startswith("pos:")))
                        after = fd.stat()
                        if ((after.st_dev, after.st_ino, after.st_size) !=
                            (before.st_dev, before.st_ino, before.st_size) or
                            os.readlink(fd) != target or
                            (proc / "comm").read_text().strip() != "sha256sum" or
                            not 0 <= position <= expected_bytes):
                            continue
                        matches.append(dict(checksum_bytes=position,
                                            checksum_pass=pass_number,
                                            checksum_passes=passes))
                except (OSError, ValueError, StopIteration, IndexError):
                    continue
        except OSError:
            continue
    return matches[0] if len(matches) == 1 else {}


def metadata(root, relative, mount, mounts):
    path = root / relative
    if storage._covering(path, mounts) != mount:
        raise ValueError("unexpected nested mount")
    for parent in reversed(path.parents):
        if parent == Path("/"):
            continue
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            if parent == root or root.is_relative_to(parent):
                raise ValueError("storage root missing")
            return None
        if not stat.S_ISDIR(mode):
            raise ValueError("non-directory or symlink in storage path")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("non-regular acquisition artifact")
    return info


def success_evidence(unit, entry, final, final_path):
    # Only current invocation evidence, never an arbitrary historical success.
    invocation = unit.get("InvocationID", "")
    if len(invocation) != 32 or any(c not in "0123456789abcdef" for c in invocation):
        return False
    try:
        import json
        records = command("journalctl", "--no-pager", "-o", "json", "-n", "30",
                          "-u", UNIT, "_SYSTEMD_INVOCATION_ID=" + invocation)
        expected = f"VERIFIED_AND_PROMOTED path={final_path} bytes={entry['bytes']} sha256={entry['sha256']}"
        for line in records.splitlines():
            record = json.loads(line)
            message = record.get("MESSAGE")
            stamp = int(record.get("__REALTIME_TIMESTAMP", "0"))
            if stamp * 1000 >= max(final.st_mtime_ns, final.st_ctime_ns) and (
                message == expected or message == f"Already verified: {final_path}"
            ):
                return True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return False


def collect(entry):
    result = {"source_id": entry["source_id"], "title": entry["title"],
              "filename": entry["filename"], "expected_bytes": entry["bytes"],
              "downloaded_bytes": None, "state": "unknown", "verified": False,
              "detail": "", "active": False}
    try:
        unit = unit_state()
        state, sub = unit.get("ActiveState"), unit.get("SubState")
        active = state in {"activating", "active", "deactivating"}
        result["active"] = active
        if unit.get("LoadState") == "not-found":
            return dict(result, state="unavailable", detail="Acquisition service not installed")
        if unit.get("LoadState") != "loaded" or state not in {
            "activating", "active", "deactivating", "inactive", "failed"
        }:
            return dict(result, detail="Service state unavailable")
        config = storage_settings.load_settings()
        # This adapter describes a specific existing external unit, not an
        # arbitrary service selected by a client. Its storage root is fixed.
        if config["storage_root"] != "/media/offline" or config["storage_mount"] != "/media/offline" or not config["storage_source"]:
            return dict(result, detail="External acquisition storage does not match saved configuration")
        root = Path(config["storage_root"])
        mounts = storage.read_mounts()
        mount = storage._covering(root, mounts)
        if (mount["target"] != config["storage_mount"] or mount["source"] != config["storage_source"]
                or mount["type"] not in {"nfs", "nfs4"}):
            return dict(result, detail="Expected NFS mount unavailable")
        name = entry["filename"]
        partial = metadata(root, Path("staging/incomplete") / (name + ".part"), mount, mounts)
        staged = metadata(root, Path("staging") / name, mount, mounts)
        final_path = root / "library/zim" / name
        final = metadata(root, Path("library/zim") / name, mount, mounts)
        if storage._covering(root, storage.read_mounts()) != mount:
            return dict(result, detail="Mount changed during sampling")
        if unit_state() != unit:
            return dict(result, detail="Service changed during sampling")
        total = entry["bytes"]
        if any(x is not None and x.st_size > total for x in (partial, staged, final)):
            return dict(result, detail="Unexpected artifact size")
        if (staged and staged.st_size != total) or (final and final.st_size != total):
            return dict(result, detail="Unexpected published or staged size")
        if partial and (staged or final):
            return dict(result, detail="Conflicting acquisition artifacts")
        artifact = final or staged or partial
        result["downloaded_bytes"] = artifact.st_size if artifact else 0
        if sub == "auto-restart":
            return dict(result, state="retrying", detail="Service will retry")
        if active:
            names = processes(unit)
            if "sha256sum" in names:
                # Normal transfer path is two passes. A restarted service
                # can verify a staged or existing final file instead; don't
                # claim a two-pass sequence for an isolated final-file check.
                candidates = [
                    (root / "staging/incomplete" / (name + ".part"), partial, 1, 2),
                    (root / "staging" / name, staged, 1, 2),
                    (final_path, final, 2 if staged else None, 2 if staged else None),
                ]
                progress = checksum_progress(unit, candidates, total)
                return dict(result, state="verifying", **progress)
            if "curl" in names and not staged and not final:
                return dict(result, state="downloading")
            if staged or final:
                return dict(result, state="publishing", detail="Awaiting service verification and exit")
            return dict(result, detail="Active service phase not confirmed")
        if state == "failed" or unit.get("Result") not in {"success", ""}:
            return dict(result, state="failed", detail="Acquisition service reported failure")
        if final:
            if unit.get("ExecMainStatus") == "0" and success_evidence(unit, entry, final, final_path):
                return dict(result, state="completed", verified=True,
                            detail="Successful acquisition service verification")
            return dict(result, detail="Published file present; verification evidence unavailable")
        if partial or staged:
            return dict(result, state="interrupted", detail="Retained artifact; service not running")
        return dict(result, state="not_started")
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        return dict(result, detail="Acquisition status could not be inspected")


class Monitor:
    """One daemon sampler; HTTP never performs NFS I/O or waits for the sampler.

    A hard NFS hang can stall this one thread indefinitely. Do not spawn more
    workers; expose stale data until it recovers or the backend restarts.
    """
    def __init__(self, entry, collector=collect):
        self.entry = entry
        self.collector = collector
        self.lock = threading.Lock()
        self.latest = None
        self.sample_time = None
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="acquisition-monitor").start()

    def run(self):
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(INTERVAL)

    def sample(self):
        try:
            data = self.collector(self.entry)
        except Exception:
            data = {"source_id": self.entry["source_id"], "title": self.entry["title"],
                    "filename": self.entry["filename"], "expected_bytes": self.entry["bytes"],
                    "state": "unknown", "downloaded_bytes": None, "verified": False,
                    "detail": "Sampling failed", "active": False}
        now = time.monotonic()
        with self.lock:
            rate = None
            if (self.latest and self.sample_time is not None and
                0 < now - self.sample_time <= STALE_AFTER and
                data["state"] == self.latest["state"] == "downloading"):
                before, after = self.latest["downloaded_bytes"], data["downloaded_bytes"]
                if before is not None and after is not None and after > before:
                    rate = (after - before) / (now - self.sample_time)
            data["speed_bytes_per_second"] = rate
            data["eta_seconds"] = (data["expected_bytes"] - data["downloaded_bytes"]) / rate if rate else None
            data["sampled_at"] = time.time()
            self.latest, self.sample_time = data, now

    def snapshot(self):
        with self.lock:
            if self.latest is None:
                return {"acquisitions": [], "stale": True}
            data = copy.deepcopy(self.latest)
            stale = time.monotonic() - self.sample_time > STALE_AFTER
        if stale:
            data.update(state="unknown", verified=False, speed_bytes_per_second=None,
                        eta_seconds=None, checksum_bytes=None, checksum_pass=None, checksum_passes=None,
                        detail="Last sample is stale; storage or service may be unavailable")
        return {"acquisitions": [data], "stale": stale}
