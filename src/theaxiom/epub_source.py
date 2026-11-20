"""Bounded EPUB 2/3 package inspection; no extraction, rendering or network access."""
from contextlib import contextmanager
import hashlib
from html import escape
import io
import posixpath
import re
import stat
import zipfile
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from . import catalog_store, pdf_source

MAX_BYTES = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBER = 8 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_RATIO = 200
CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
XHTML = "{http://www.w3.org/1999/xhtml}"


def _safe_name(name):
    return (bool(name) and len(name) <= 512 and not name.startswith("/")
            and not any(ord(c) < 32 or ord(c) == 127 or c in "\\:%?#" for c in name)
            and all(part not in {"", ".", ".."} for part in name.split("/")))


def _resolve(base, href):
    try:
        url = urlsplit(href)
        path = unquote(url.path, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid EPUB resource reference") from exc
    if (url.scheme or url.netloc or url.query or url.fragment
            or not _safe_name(path)):
        raise ValueError("Unsafe EPUB resource reference")
    result = posixpath.join(posixpath.dirname(base), path)
    if not _safe_name(result):
        raise ValueError("Unsafe EPUB resource path")
    return result


def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_XML:
        raise ValueError("EPUB XML exceeds limit")
    raw = archive.read(info)
    # Restrict to UTF-8 XML to make the DTD/entity prohibition unambiguous.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("EPUB XML must be UTF-8") from exc
    if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("EPUB DTDs and entities are unsupported")
    declaration = re.match(r"\s*<\?xml\b[^?]*encoding\s*=\s*['\"]([^'\"]+)", text, re.I)
    if declaration and declaration[1].lower() not in {"utf-8", "utf8"}:
        raise ValueError("Unsupported EPUB XML encoding")
    root = ET.fromstring(text)
    stack = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 64 or count > 50000:
            raise ValueError("EPUB XML complexity exceeds limit")
        stack.extend((child, depth + 1) for child in node)
    return root


def inspect(data):
    """Return metadata and ordered chapter paths for a bounded EPUB byte string.

    No archive data is written to disk. Reader rendering must separately enforce
    a safe output allowlist; validation does not authorize publication.
    """
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError("EPUB exceeds compressed size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_ENTRIES:
                raise ValueError("EPUB entry count exceeds limit")
            first = entries[0]
            if (first.filename != "mimetype" or first.header_offset != 0
                    or first.compress_type != zipfile.ZIP_STORED or first.extra
                    or first.file_size != 20):
                raise ValueError("Missing EPUB mimetype header")
            names, folded, total = set(), set(), 0
            for member in entries:
                name = member.filename
                path = name[:-1] if member.is_dir() else name
                mode = stat.S_IFMT(member.external_attr >> 16)
                if (member.orig_filename != name or not _safe_name(path)
                        or path.casefold() in folded
                        or mode not in {0, stat.S_IFDIR if member.is_dir() else stat.S_IFREG}
                        or member.flag_bits & 1
                        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or member.file_size > MAX_MEMBER
                        or member.file_size > MAX_RATIO * max(1, member.compress_size)):
                    raise ValueError("Unsafe EPUB archive entry")
                folded.add(path.casefold())
                total += member.file_size
                if total > MAX_EXPANDED:
                    raise ValueError("EPUB expanded size exceeds limit")
                if not member.is_dir():
                    names.add(name)
            if archive.read(first) != b"application/epub+zip":
                raise ValueError("Invalid EPUB mimetype")
            if {"META-INF/encryption.xml", "META-INF/rights.xml"} & names:
                raise ValueError("Encrypted or DRM-protected EPUB is unsupported")
            if archive.testzip() is not None:
                raise ValueError("EPUB archive checksum failed")
            container = _xml(archive, "META-INF/container.xml")
            roots = container.findall(CONTAINER + "rootfiles/" + CONTAINER + "rootfile")
            if container.tag != CONTAINER + "container" or len(roots) != 1:
                raise ValueError("EPUB must contain one package")
            package_path = roots[0].get("full-path", "")
            if not _safe_name(package_path) or roots[0].get("media-type") != "application/oebps-package+xml":
                raise ValueError("Invalid EPUB package path")
            package = _xml(archive, package_path)
            version = package.get("version", "")
            if package.tag != OPF + "package" or version not in {"2.0", "3.0"}:
                raise ValueError("Only EPUB 2 and 3 packages are supported")
            metadata = package.find(OPF + "metadata")
            manifest = package.find(OPF + "manifest")
            spine = package.find(OPF + "spine")
            if metadata is None or manifest is None or spine is None:
                raise ValueError("Incomplete EPUB package")
            fields = {}
            for field, tag in (("title", "title"), ("author", "creator"),
                               ("language", "language"), ("publisher", "publisher")):
                values = [" ".join("".join(node.itertext()).split())[:512]
                          for node in metadata.findall(DC + tag)]
                fields[field] = "; ".join(v for v in values if v)[:512]
            items = {}
            for item in manifest.findall(OPF + "item"):
                key = item.get("id", "")
                path = _resolve(package_path, item.get("href", ""))
                media = item.get("media-type", "")
                if (not key or key in items or path not in names
                        or "scripted" in item.get("properties", "").split()
                        or media in {"application/javascript", "text/javascript"}):
                    raise ValueError("Invalid or scripted EPUB manifest")
                items[key] = (path, media)
            chapters = []
            for item in spine.findall(OPF + "itemref"):
                path, media = items[item.get("idref", "")]
                if media != "application/xhtml+xml":
                    raise ValueError("Unsupported EPUB spine resource")
                chapter = _xml(archive, path)
                if chapter.tag != XHTML + "html" or chapter.find(XHTML + "body") is None:
                    raise ValueError("Invalid EPUB chapter")
                for element in chapter.iter():
                    if element.tag.split("}")[-1].lower() in {
                        "script", "iframe", "object", "embed", "form", "base"
                    } or any(key.split("}")[-1].lower().startswith("on") for key in element.attrib):
                        raise ValueError("Active EPUB chapter content is unsupported")
                chapters.append(path)
            if not chapters:
                raise ValueError("EPUB has no readable chapters")
            return dict(fields, version=version, chapters=chapters)
    except (KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError,
            NotImplementedError, OSError, UnicodeError, EOFError) as exc:
        raise ValueError("Invalid EPUB archive or package") from exc


def pin(source_id, config, *, require_rights=True):
    if config.get("format") != "epub":
        raise ValueError("Expected EPUB format")
    artifact = pdf_source.pin(source_id, dict(config, format="pdf"),
                              require_rights=require_rights)
    return dict(artifact, filename=artifact["filename"][:-4] + ".epub")


def _verify(stream, artifact):
    size = artifact["bytes"]
    if not 0 < size <= MAX_BYTES:
        raise ValueError("Invalid EPUB pin size")
    data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ValueError("EPUB size or SHA-256 does not match pin")
    metadata = inspect(data)
    stream.seek(0)
    return metadata


def install_stream(source_id, stream, *, expected_size=None, expected_sha256=None):
    metadata = {}

    def validate(staged, artifact):
        metadata.update(_verify(staged, artifact))

    artifact = pdf_source._install_stream(
        source_id, stream, expected_size=expected_size, expected_sha256=expected_sha256,
        format_name="epub", verify=validate)
    return artifact, metadata


@contextmanager
def installed(artifact, *, verify=True):
    filename = artifact["filename"]
    if not re.fullmatch(r"[a-z0-9-]{1,63}\.[a-f0-9]{64}\.epub", filename):
        raise ValueError("Invalid EPUB artifact filename")
    with pdf_source._directory("library", format_name="epub") as (directory, mount, _):
        with pdf_source._file(directory, mount, filename, artifact["bytes"]) as stream:
            if verify:
                _verify(stream, artifact)
            yield stream


def ready(artifact):
    if artifact:
        try:
            with installed(artifact):
                return True
        except (OSError, ValueError):
            pass
    return False


ROUTE_PREFIX = "/api/library/epub/"
ROUTE = re.compile(ROUTE_PREFIX + r"([a-z0-9-]{1,63})(?:/(document\.epub|chapter/[0-9]{1,4}))?(?:\?size=(small|medium|large))?\Z")
# Only literal trusted tags are emitted. No original HTML attributes, CSS,
# hyperlinks, SVG, images, or active content enter the application's origin.
SAFE_TAGS = frozenset("p div section article h1 h2 h3 h4 h5 h6 blockquote pre code em strong b i u s sub sup ul ol li dl dt dd table thead tbody tfoot tr th td caption br hr span".split())
DROP_TAGS = frozenset("script style iframe object embed form base head link meta svg math audio video".split())


def chapter_content(data, index):
    metadata = inspect(data)
    if not 0 <= index < len(metadata["chapters"]):
        raise ValueError("Unknown EPUB chapter")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        def safe(node):
            name = node.tag[len(XHTML):] if node.tag.startswith(XHTML) else ""
            if not name or name in DROP_TAGS:
                return ""
            if name == "img":
                return escape(node.get("alt", ""))
            content = escape(node.text or "") + "".join(
                safe(child) + escape(child.tail or "") for child in node)
            if name in {"br", "hr"}:
                return "<" + name + ">"
            if name in SAFE_TAGS:
                return "<" + name + ">" + content + "</" + name + ">"
            return content

        chapter = _xml(archive, metadata["chapters"][index])
        labels = []
        for path in metadata["chapters"]:
            root = _xml(archive, path)
            heading = next((node for node in root.iter()
                            if node.tag in {XHTML + "h1", XHTML + "h2", XHTML + "h3"}
                            and "".join(node.itertext()).strip()), None)
            label = " ".join("".join(heading.itertext()).split())[:100] if heading is not None else ""
            labels.append(label or f"Chapter {len(labels) + 1}")
        return safe(chapter.find(XHTML + "body")), labels


def status(record, artifact):
    from . import publication
    path = ROUTE_PREFIX + record["source_id"]
    result = dict(enabled=False, verified=False, serving=False, published=False,
                  path=path, entry_path=path, publication_reason="")
    try:
        enabled = publication.load().get(record["source_id"], False)
        available = ready(artifact)
        rights = bool(record["configuration"].get("license", "").strip())
        return dict(result, enabled=enabled, verified=available, serving=available and rights,
                    published=enabled and available and rights,
                    publication_reason="Review usage rights before publishing" if available and not rights else "",
                    requires_rights_acknowledgement=available and not rights)
    except (OSError, ValueError):
        return dict(result, error="publication state unavailable")
