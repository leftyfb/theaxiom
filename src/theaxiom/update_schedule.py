"""Persistent operator schedule for metadata-only update checks."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Lock
from zoneinfo import ZoneInfo

from . import storage_settings

LOCK = Lock()
DEFAULT = {"interval_days": 1, "start_time": "03:00"}


def state_path():
    return storage_settings.SETTINGS_PATH.with_name("update-schedule.json")


def timezone():
    with Path("/etc/localtime").open("rb") as stream:
        return ZoneInfo.from_file(stream)


def validate(value):
    if (not isinstance(value, dict) or value.keys() != DEFAULT.keys()
            or type(value["interval_days"]) is not int or not 1 <= value["interval_days"] <= 365
            or not isinstance(value["start_time"], str)
            or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value["start_time"])):
        raise ValueError("Use an interval of 1–365 days and a valid start time.")
    return value


def _read():
    try:
        with state_path().open() as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return None
    validate(state["settings"])
    date.fromisoformat(state["anchor"])
    return state


def _write(state, path=None):
    path = path or state_path()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".update-schedule-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def automatic_paused():
    path = state_path().with_name("automatic-update-pause.json")
    try:
        value = json.loads(path.read_text())
        return value is not False
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def set_automatic_pause(paused):
    path = state_path().with_name("automatic-update-pause.json")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(paused, path)


def save(value, now=None):
    validate(value)
    now = now or datetime.now(timezone())
    with LOCK:
        previous = _read()
        if (previous["settings"] if previous else DEFAULT) == value:
            return
        anchor = now.date()
        if now.time() >= time.fromisoformat(value["start_time"]):
            anchor += timedelta(days=1)
        _write({"settings": value, "anchor": anchor.isoformat(), "completed": None})


def slot(state, now):
    """Return the last due slot (if unfinished) and the following slot."""
    anchor = date.fromisoformat(state["anchor"])
    interval = timedelta(days=state["settings"]["interval_days"])
    start = time.fromisoformat(state["settings"]["start_time"])
    elapsed_days = max(0, (now.date() - anchor).days)
    latest = datetime.combine(anchor + interval * (elapsed_days // interval.days), start, now.tzinfo)
    if latest.timestamp() > now.timestamp():
        latest -= interval
    upcoming = latest + interval
    due = latest.date() >= anchor and latest.date().isoformat() != state["completed"]
    return latest if due else None, upcoming


def current():
    with LOCK:
        return _read()


def complete(state, finished_slot):
    with LOCK:
        if state == _read():
            _write(dict(state, completed=finished_slot.date().isoformat()))


def _legacy_next(zone):
    try:
        result = subprocess.run(
            ["systemctl", "show", "theaxiom-update-check.timer",
             "--property=NextElapseUSecRealtime", "--value", "--timestamp=unix"],
            check=True, capture_output=True, text=True, timeout=2,
        )
        return datetime.fromtimestamp(float(result.stdout.strip().removeprefix("@")), zone)
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return None


def status():
    zone = timezone()
    now = datetime.now(zone)
    state = current()
    if state:
        due, upcoming = slot(state, now)
        next_run = due or upcoming
        settings = state["settings"]
    else:
        next_run = _legacy_next(zone)
        settings = DEFAULT
    zone_name = os.path.realpath("/etc/localtime").partition("/zoneinfo/")[2]
    return dict(settings, time_zone=zone_name or now.strftime("%Z (%z)"),
                next_run=datetime.fromtimestamp(next_run.timestamp(), zone).isoformat() if next_run else None)
