"""Read the packaged starter source catalog."""
from copy import deepcopy
from importlib.resources import files
import json

_CATALOG = json.loads(files(__package__).joinpath("config.json").read_text(encoding="utf-8"))
SEED_CATALOG_VERSION = _CATALOG["version"]


def builtin_sources():
    """Return a detached copy of the starter catalog."""
    return deepcopy(_CATALOG["sources"])


def seed_catalog_manifest():
    return deepcopy(_CATALOG)


def planned_sources():
    return deepcopy([entry for entry in _CATALOG["sources"] if entry["kind"] == "planned"])


# Legacy fixture consumers use these aliases.
_BY_ID = {entry["source_id"]: entry for entry in builtin_sources()}
WIKIPEDIA_SOURCE = _BY_ID["wikipedia-en"]
READY_GOV_SOURCE = _BY_ID["fema-ready"]
CONFIGURED_PDFS = {sid: entry["setup"] for sid, entry in _BY_ID.items()
                   if entry.get("setup", {}).get("format") == "pdf"}


def source_defaults():
    return deepcopy(_BY_ID)
