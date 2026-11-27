"""Bounded JSON/CSV validation and offline, publication-gated record browsing."""
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import json
import math
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

FORMATS = {"json", "csv"}
CONFIG_FIELDS = ("data_record_path", "data_columns", "data_title_field")
MEDIA_TYPES = {"json": "application/json", "csv": "text/csv"}
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 48
MAX_CELL = 4096
MAX_CELLS = 1_000_000
PAGE_SIZE = 20
ROUTE = re.compile(r"/api/library/data/([a-z0-9-]{1,63})(?:/record/([a-f0-9]{64})/([1-9][0-9]{0,5}))?\Z")
_READERS = threading.BoundedSemaphore(2)


def options(config):
    values = [config.get(key, "") for key in CONFIG_FIELDS]
    if not all(isinstance(v, str) and len(v) <= 512 and not any(ord(c) < 32 for c in v)
               for v in values):
        raise ValueError("Invalid structured data configuration")
    path, columns, title = values
    columns = tuple(c.strip() for c in columns.split(",")) if columns else ()
    if (len(columns) > MAX_COLUMNS or len(set(columns)) != len(columns)
            or any(not c or len(c) > 128 for c in columns) or len(title) > 128):
        raise ValueError("Use distinct nonempty data column names")
    if config["format"] == "csv" and path:
        raise ValueError("A JSON record path is not applicable to CSV")
    if config["format"] == "json":
        for field in (*columns, *((path,) if path else ()), *((title,) if title else ())):
            parts = field.split(".")
            if len(parts) > 8 or any(not p for p in parts):
                raise ValueError("Invalid dot-separated data field path")
    if title and columns and title not in columns:
        raise ValueError("Record title must be a displayed column")
    return path, columns, title


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("Non-finite JSON number")


def _at(obj, path):
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite data value")
    if not isinstance(value, (str, int, float)):
        raise ValueError("Displayed data columns must contain scalar values")
    text = str(value)
    if (len(text) > MAX_CELL or any(0xD800 <= ord(c) <= 0xDFFF or
            (ord(c) < 32 and c not in "\n\r\t") for c in text)):
        raise ValueError("Data cell exceeds text limits")
    return text


@dataclass(frozen=True)
class Dataset:
    columns: tuple
    rows: tuple
    title_index: int

    def title(self, index, row):
        if self.title_index >= 0 and row[self.title_index].strip():
            return row[self.title_index].strip()[:300]
        return next((cell.strip()[:300] for cell in row if cell.strip()), f"Record {index}")

    def matching(self, query, deadline=None):
        needle = query.casefold()
        for index, row in enumerate(self.rows, 1):
            if deadline is not None and index % 128 == 1 and time.monotonic() >= deadline:
                raise TimeoutError("Data search time budget used")
            if not needle or any(needle in cell.casefold() for cell in row):
                yield index, row


def parse(stream, config):
    """Read a bounded UTF-8 snapshot; reject malformed or oversized data before promotion."""
    path, selected, title = options(config)
    raw = stream.read(MAX_BYTES + 1)
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Structured data exceeds the 16 MiB download limit")
    try:
        text = raw.decode("utf-8-sig")
        if config["format"] == "json":
            root = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
            records = _at(root, path) if path else root
            if not isinstance(records, list) or not 0 < len(records) <= MAX_ROWS:
                raise ValueError("Expected a nonempty JSON array of at most 50,000 records")
            if not all(isinstance(row, dict) for row in records):
                raise ValueError("JSON records must be objects")
            if selected:
                columns = selected
                if not all(any(_at(row, c) is not None for row in records) for c in columns):
                    raise ValueError("Configured data column not found or entirely null")
            else:
                names = {}
                for row in records:
                    for key, value in row.items():
                        if isinstance(value, (str, int, float, bool)) or value is None:
                            if not key.strip() or len(key) > 128 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in key):
                                raise ValueError("Invalid data column name")
                            names[key] = None
                            if len(names) > MAX_COLUMNS:
                                raise ValueError("Too many data columns; select columns explicitly")
                columns = tuple(names)
            if not columns or len(columns) * len(records) > MAX_CELLS:
                raise ValueError("Data exceeds displayed cell limit")
            if selected:
                rows = tuple(tuple(_text(_at(row, c)) for c in columns) for row in records)
            else:
                rows = tuple(tuple(_text(row.get(c)) if not isinstance(row.get(c), (dict, list)) else ""
                                   for c in columns) for row in records)
        elif config["format"] == "csv":
            records = csv.reader(io.StringIO(text, newline=""), strict=True)
            header = next(records, [])
            if (not header or len(header) > 256 or len(set(header)) != len(header)
                    or any(not key.strip() or len(key) > 128 or any(ord(c) < 32 for c in key) for key in header)):
                raise ValueError("CSV requires unique, nonempty header names")
            columns = selected or tuple(header)
            if len(columns) > MAX_COLUMNS or any(c not in header for c in columns):
                raise ValueError("Select valid CSV columns")
            indices = [header.index(c) for c in columns]
            rows = []
            for row in records:
                if not row:
                    continue
                if (len(row) != len(header) or len(rows) >= MAX_ROWS
                        or (len(rows) + 1) * len(columns) > MAX_CELLS):
                    raise ValueError("Invalid CSV row width or dataset exceeds limits")
                rows.append(tuple(_text(row[i]) for i in indices))
            rows = tuple(rows)
        else:
            raise ValueError("Unsupported data format")
    except (csv.Error, RecursionError, UnicodeError, OverflowError) as exc:
        raise ValueError("Malformed UTF-8 structured data") from exc
    if not rows or not columns or not any(any(cell.strip() for cell in row) for row in rows):
        raise ValueError("Dataset contains no readable records")
    if title and title not in columns:
        raise ValueError("Record title field is not a displayed column")
    return Dataset(columns, rows, columns.index(title) if title else 0)


def _installed(record):
    from . import zim_jobs
    sid = record["source_id"]
    job = zim_jobs.status(sid)
    if (not job or job.get("verified") is not True or job.get("active") is not False
            or job["configuration"]["format"] != record["configuration"]["format"]
            or job["configuration"]["format"] not in FORMATS):
        return None
    artifact = zim_jobs.installed(job, job["configuration"])
    if artifact:
        return job, artifact
    return None


def status(record):
    from . import publication
    sid = record["source_id"]
    path = "/api/library/data/" + sid
    result = dict(enabled=False, verified=False, serving=False, published=False,
                  path=path, entry_path=path, publication_reason="")
    try:
        enabled = publication.load().get(sid, False)
        verified = _installed(record) is not None
        rights = bool(record["configuration"].get("license", "").strip())
        ready = enabled and verified and rights
        return dict(result, enabled=enabled, verified=verified, serving=ready, published=ready,
                    ready_for_publication=verified, requires_rights_acknowledgement=verified and not rights)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(result, error="Data publication state unavailable")


def current(catalog, sid):
    """Bind reader/search to the serving digest and installed schema, not a staged edit."""
    from . import publication
    try:
        with publication.LOCK:
            record = next((r for r in catalog.get()["sources"] if r["source_id"] == sid
                           and r["configuration"].get("format") in FORMATS), None)
            if not record or not publication.load().get(sid) or not record["configuration"].get("license", "").strip():
                return None
            installed = _installed(record)
            if installed:
                job, artifact = installed
                return dict(source_id=sid, configuration=job["configuration"], artifact=artifact,
                            profile=record["profile"], license=record["configuration"]["license"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


@contextmanager
def open_dataset(snapshot):
    from . import pdf_source, zim_jobs
    if not _READERS.acquire(blocking=False):
        raise RuntimeError("Data reader busy")
    try:
        artifact = snapshot["artifact"]
        config = snapshot["configuration"]
        if not 0 < artifact["bytes"] <= MAX_BYTES:
            raise ValueError("Invalid dataset size")
        with pdf_source._directory("library", format_name=config["format"]) as (fd, mount, _):
            with zim_jobs.open_installed_archive(fd, mount, snapshot, artifact) as stream:
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) != artifact["bytes"] or sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError("Data snapshot integrity mismatch")
        yield parse(io.BytesIO(raw), config)
    finally:
        _READERS.release()


def reader_params(path, digest, number):
    from .search import validate_query
    if len(path) > 4096:
        raise ValueError("Invalid data reader request")
    params = parse_qs(urlsplit(path).query, keep_blank_values=True, max_num_fields=3, errors="strict")
    if (set(params) - {"q", "page", "view"} or any(len(v) != 1 for v in params.values())
            or (digest and params)):
        raise ValueError("Invalid data reader parameters")
    query = validate_query(params.get("q", [""])[0])
    page = params.get("page", ["1"])[0]
    view = params.get("view", ["cards"])[0]
    if not re.fullmatch(r"[1-9][0-9]{0,3}", page) or view not in {"cards", "table"}:
        raise ValueError("Invalid data page or view")
    return query, int(page), view, digest, int(number) if number else None


def record_path(path, snapshot, index):
    return f'{path}/record/{snapshot["artifact"]["sha256"]}/{index}'


def reader_context(dataset, snapshot, params):
    query, page, view, digest, number = params
    path = "/api/library/data/" + snapshot["source_id"]
    if digest:
        if digest != snapshot["artifact"]["sha256"] or not 1 <= number <= len(dataset.rows):
            raise ValueError("Record no longer available")
        entries = [(number, dataset.rows[number - 1])]
        total = 1
        view = "cards"
    else:
        start = (page - 1) * PAGE_SIZE
        entries = []
        total = 0
        for index, row in dataset.matching(query, time.monotonic() + 5):
            if start <= total < start + PAGE_SIZE:
                entries.append((index, row))
            total += 1
    def link(target_page, target_view):
        return path + "?" + urlencode(dict(q=query, page=target_page, view=target_view))
    return dict(profile=snapshot["profile"], license=snapshot["license"], path=path,
                query=query, view=view, detail=bool(digest), page=page, total=total,
                row_count=len(dataset.rows), columns=dataset.columns,
                entries=[dict(index=i, title=dataset.title(i, row), cells=row,
                              href=record_path(path, snapshot, i)) for i, row in entries],
                cards_url=link(page, "cards"), table_url=link(page, "table"),
                previous=link(page - 1, view) if not digest and page > 1 else "",
                following=link(page + 1, view) if not digest and page * PAGE_SIZE < total else "")


def search_records(route, query):
    from .search import Result, Page, MAX_RESULTS
    snapshot = route["snapshot"]
    offset = route.get("offset", 0)
    results = []
    with open_dataset(snapshot) as dataset:
        for position, (index, row) in enumerate(dataset.matching(query, route.get("deadline"))):
            if position < offset:
                continue
            if len(results) == MAX_RESULTS:
                return Page(results, has_more=True)
            snippet = " · ".join(f"{column}: {cell[:160]}" for column, cell in zip(dataset.columns, row) if cell)
            results.append(Result(dataset.title(index, row), record_path(route["path"], snapshot, index), snippet[:600]))
    return Page(results)
