"""Private, digest-bound document text indexes for offline library search."""
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from . import epub_source, pdf_source, storage_settings

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 240_000
MAX_SECTIONS = 1500
WORKER = threading.Lock()
RETRY_AFTER = 60
_failed = None


class IndexPending(ValueError):
    pass


class NoText(ValueError):
    pass


def _path(artifact):
    sid, _, _ = artifact["filename"].split(".")
    return storage_settings.SETTINGS_PATH.with_name("document-search-v1") / (sid + ".json")


def _load(artifact):
    target = _path(artifact)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                    or info.st_size > MAX_INDEX_BYTES):
                return None
            raw = stream.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            return None
        data = json.loads(raw)
        identity = [artifact["filename"], artifact["sha256"], artifact["bytes"]]
        if (data["artifact"] != identity or not isinstance(data["sections"], list)
                or len(data["sections"]) > MAX_SECTIONS or type(data["partial"]) is not bool):
            return None
        for number, text in data["sections"]:
            if type(number) is not int or not 1 <= number <= MAX_SECTIONS or not isinstance(text, str):
                return None
        return data
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _index(artifact, kind, target):
    global _failed
    try:
        adapter = pdf_source if kind == "pdf" else epub_source
        with adapter.installed(artifact, verify=False) as stream:
            data = stream.read(artifact["bytes"] + 1)
        if len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("Document changed")
        completed = subprocess.run(
            [sys.executable, "-m", "theaxiom.document_search", kind],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=12, check=True,
        )
        extracted = json.loads(completed.stdout)
        extracted["artifact"] = [artifact["filename"], artifact["sha256"], artifact["bytes"]]
        raw = json.dumps(extracted, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError("Document index exceeds size limit")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".index-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    except (OSError, ValueError, subprocess.SubprocessError):
        _failed = (target, artifact["sha256"], time.monotonic() + RETRY_AFTER)
    finally:
        WORKER.release()


def schedule(artifact, kind):
    if artifact and _load(artifact) is None and WORKER.acquire(blocking=False):
        target = _path(artifact)
        if _failed and _failed[:2] == (target, artifact["sha256"]) and time.monotonic() < _failed[2]:
            WORKER.release()
            return
        worker = threading.Thread(target=_index, args=(artifact, kind, target),
                                  name="document-search-index", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            WORKER.release()


def search_document(route, query):
    from .search import MAX_RESULTS, Page, Result

    artifact, kind = route["artifact"], route["kind"]
    skip = route.get("offset", 0)
    needle = query.casefold()
    # Catalog metadata is already local and publication-gated by the shared
    # coordinator. It must remain searchable even for image-only documents.
    metadata = " · ".join(value for value in (
        route.get("name", ""), route.get("author", ""), route.get("description", "")
    ) if value)
    metadata_match = needle in metadata.casefold()
    matches = []
    if metadata_match:
        if skip:
            skip -= 1
        else:
            href = route["path"] + ("#page=1" if kind == "pdf" else "/chapter/0")
            matches.append(Result(f"{route['name']} · Document details", href, metadata[:600]))
    data = _load(artifact)
    if data is None:
        schedule(artifact, kind)
        if metadata_match:
            return Page(matches, partial=True)
        if _failed and _failed[:2] == (_path(artifact), artifact["sha256"]) and time.monotonic() < _failed[2]:
            raise ValueError("Document indexing temporarily unavailable")
        raise IndexPending
    if not data["sections"]:
        if metadata_match:
            return Page(matches, partial=data["partial"])
        raise NoText
    for number, content in data["sections"]:
        offset = content.casefold().find(needle)
        if offset < 0:
            continue
        if skip:
            skip -= 1
            continue
        if len(matches) == MAX_RESULTS:
            return Page(matches, has_more=True, partial=data["partial"])
        href = route["path"] + (f"#page={number}" if kind == "pdf" else f"/chapter/{number - 1}")
        snippet = content[max(0, offset - 70):offset + len(query) + 100]
        label = "Page" if kind == "pdf" else "Chapter"
        matches.append(Result(f"{route['name']} · {label} {number}", href, snippet))
    return Page(matches, partial=data["partial"])


def _sections(data, kind):
    if kind == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return [], False
        pages = reader.pages
        texts = (pages[i].extract_text() or "" for i in range(min(len(pages), MAX_SECTIONS)))
        return _bound(texts, len(pages))
    metadata = epub_source.inspect(data)
    chapters = metadata["chapters"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        def chapter_text(path):
            root = epub_source._xml(archive, path)
            body = root.find(epub_source.XHTML + "body")
            if body is None:
                return ""
            for parent in body.iter():
                for child in list(parent):
                    if child.tag.rsplit("}", 1)[-1] in epub_source.DROP_TAGS:
                        parent.remove(child)
            return " ".join(body.itertext())

        return _bound((chapter_text(path) for path in chapters[:MAX_SECTIONS]), len(chapters))


def _bound(texts, total):
    sections = []
    remaining = MAX_TEXT
    for number, text in enumerate(texts, 1):
        text = " ".join(text.split())
        if text:
            sections.append((number, text[:remaining]))
            remaining -= len(text)
        if remaining <= 0:
            return sections, remaining < 0 or number < total
    return sections, total > MAX_SECTIONS


def _main():
    import resource

    memory = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    kind = sys.argv[1]
    data = sys.stdin.buffer.read(pdf_source.MAX_BYTES + 1)
    if kind not in {"pdf", "epub"} or not 0 < len(data) <= pdf_source.MAX_BYTES:
        return 1
    sections, partial = _sections(data, kind)
    print(json.dumps(dict(sections=sections, partial=partial)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
