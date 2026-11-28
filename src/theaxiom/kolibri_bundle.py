"""Build a static, scoped educational archive from an official Kolibri export.

No embedded third-party HTML, executable exercises, external playback URLs, or
unselected media are copied. The caller owns the staging directory and storage
checks; the resulting ZIM must pass the normal archive verification lifecycle.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
import hashlib
from html import escape
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import tempfile
import time
import zlib

from . import kolibri_scope as scope, pdf_transport

STUDIO = "https://studio.learningequality.org/content/"
SUPPORTED = {"mp4", "webm", "mp3", "ogg", "wav", "jpg", "jpeg", "png", "webp", "gif", "pdf", "vtt"}
PLAYABLE = SUPPORTED - {"vtt"}
MAX_CAPTION = 2 * 1024 * 1024
MAX_METADATA = scope.MAX_DATABASE_BYTES


@dataclass(frozen=True)
class Lesson:
    node_id: str
    parent_id: str
    title: str
    description: str
    license: str
    author: str
    files: tuple


@dataclass(frozen=True)
class Bundle:
    channel: scope.Channel
    topics: tuple
    lessons: tuple
    files: tuple
    total_bytes: int
    omitted: int


def select(path, channel_id, nodes, *, allow_full=False):
    """Select supported, licensed static resources and their ancestor topics."""
    plan = scope.plan_scope(path, channel_id, nodes, allow_full=allow_full)
    try:
        with closing(scope._connect(path)) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(content_contentnode)")}
            description = "description" if "description" in columns else "''"
            author = "author" if "author" in columns else "''"
            if "license_owner" in columns:
                author = "COALESCE(NULLIF(license_owner, ''), " + author + ")"
            rows = conn.execute(
                "SELECT id,parent_id,title,kind,license_name,tree_id,lft,rght,"
                + description + "," + author +
                " FROM content_contentnode WHERE channel_id=? LIMIT ?",
                (channel_id, scope.MAX_RESOURCES + 1),
            ).fetchall()
            if len(rows) > scope.MAX_RESOURCES:
                raise scope.ScopeError("Channel exceeds static renderer node limit.")
            by_id = {scope._id(r[0]): r for r in rows}
            selected = [by_id[n] for n in plan.selected_nodes]
            candidates = [r for r in rows if r[3] != "topic" and any(
                r[5] == n[5] and r[6] >= n[6] and r[7] <= n[7] for n in selected)]
            files = {f.checksum: f for f in plan.files if f.extension in SUPPORTED}
            links = defaultdict(list)
            for node, checksum in conn.execute(
                "SELECT f.contentnode_id,f.local_file_id FROM content_file f "
                "JOIN content_contentnode n ON n.id=f.contentnode_id WHERE n.channel_id=?",
                (channel_id,),
            ):
                if checksum in files:
                    links[node].append(files[checksum])
            lessons = []
            topics = {}
            used = {}
            for row in candidates:
                node, parent, title, kind, license_name, *_, desc, owner = row
                if kind not in {"video", "audio", "document", "image", "pdf"}:
                    continue
                media = tuple(dict.fromkeys(links[node]))
                if not license_name or not any(f.extension in PLAYABLE for f in media):
                    continue
                # Unknown and reserved-rights materials require separate review;
                # this renderer accepts only explicitly identified open licenses.
                if not re.match(r"^(CC BY(?:[- ]|$)|CC0(?:[- ]|$)|Public Domain$)", license_name, re.I):
                    continue
                parent = scope._id(parent)
                ancestors = []
                cursor = parent
                seen = {node}
                while cursor:
                    if cursor in seen or cursor not in by_id or by_id[cursor][3] != "topic":
                        raise scope.ScopeError("Invalid topic ancestry.")
                    seen.add(cursor)
                    topic = by_id[cursor]
                    ancestors.append(topic)
                    cursor = topic[1]
                if not ancestors or ancestors[-1][0] != plan.channel.root_id:
                    raise scope.ScopeError("Lesson not attached to the channel root.")
                for topic in ancestors:
                    topics[topic[0]] = topic
                lessons.append(Lesson(node, parent, str(title), str(desc or ""), license_name, str(owner or ""), media))
                for file in media:
                    used[file.checksum] = file
            if not lessons:
                raise scope.ScopeError("No supported, openly licensed static resources in selection.")
            ordered_topics = tuple((r[0], r[1], str(r[2])) for r in sorted(topics.values(), key=lambda r: r[6]))
            return Bundle(plan.channel, ordered_topics, tuple(lessons), tuple(used.values()),
                          sum(f.size for f in used.values()), len(candidates) - len(lessons))
    except sqlite3.Error as exc:
        raise scope.ScopeError("Unsupported channel database schema.") from exc


def fetch_metadata(channel_id, target, *, version=None, digest=None):
    channel_id = scope._id(channel_id)
    if version is not None and (type(version) is not int or version < 0):
        raise scope.ScopeError("Invalid channel version.")
    suffix = "" if version is None else "-" + str(version)
    url = STUDIO + "databases/" + channel_id + suffix + ".sqlite3"
    sha = hashlib.sha256()
    with pdf_transport.open_pdf(url, format_name="zim", limit=MAX_METADATA, total_seconds=180) as stream:
        with Path(target).open("xb") as out:
            while block := stream.read(1024 * 1024):
                sha.update(block)
                out.write(block)
    actual = sha.hexdigest()
    if digest is not None and actual != digest:
        raise scope.ScopeError("Channel metadata changed since selection.")
    channel = scope.inspect_channel(target, channel_id)
    if version is not None and channel.version != version:
        raise scope.ScopeError("Channel version changed since selection.")
    return channel, actual


def _media_name(file):
    return "media/" + file.checksum + "." + file.extension


def _page(title, body):
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + escape(title) + '</title><style>'
            'body{font:1rem system-ui;max-width:60rem;margin:auto;padding:1rem}'
            'a{overflow-wrap:anywhere}li{margin:.5rem 0}video,img,audio{max-width:100%}'
            'video{width:100%;height:auto}pre{white-space:pre-wrap}</style></head><body>'
            '<h1>' + escape(title) + '</h1>' + body + '</body></html>')


def _link(node, title):
    return '<li><a href="' + node + '.html">' + escape(title) + '</a></li>'


def _caption(path):
    if path.stat().st_size > MAX_CAPTION:
        return ""
    data = path.read_text(encoding="utf-8-sig", errors="replace")
    if not data.startswith("WEBVTT"):
        return ""
    # Preserve searchable spoken text, not timestamps or cue styling.
    lines = [line for line in data.splitlines()[1:] if line.strip() and
             "-->" not in line and not line.strip().isdigit()]
    return "\n".join(lines)


def render(bundle, directory, *, publisher_notice=""):
    """Render local pages only, retaining hierarchy and source attribution."""
    directory = Path(directory)
    children = defaultdict(list)
    for node, parent, title in bundle.topics:
        if parent:
            children[parent].append(_link(node, title))
    for lesson in bundle.lessons:
        children[lesson.parent_id].append(_link(lesson.node_id, lesson.title))
        body = '<p><a href="' + lesson.parent_id + '.html">Back to topic</a></p>'
        body += "<p>" + escape(lesson.description) + "</p>"
        captions = [f for f in lesson.files if f.extension == "vtt"]
        for file in lesson.files:
            src = _media_name(file)
            ext = file.extension
            if ext in {"mp4", "webm"}:
                body += '<video controls preload="metadata" src="' + src + '">'
                for caption in captions:
                    body += '<track kind="captions" label="Captions" src="' + _media_name(caption) + '">'
                body += "</video>"
            elif ext in {"mp3", "ogg", "wav"}:
                body += '<audio controls preload="metadata" src="' + src + '"></audio>'
            elif ext in {"jpg", "jpeg", "png", "webp", "gif"}:
                body += '<img loading="lazy" alt="" src="' + src + '">'
            elif ext == "pdf":
                body += '<p><a href="' + src + '">Read PDF</a></p>'
        for caption in captions:
            transcript = _caption(directory / _media_name(caption))
            if transcript:
                body += "<h2>Transcript</h2><pre>" + escape(transcript) + "</pre>"
        body += "<p>License: " + escape(lesson.license) + ". Author: " + escape(lesson.author) + "</p>"
        body += "<p>Source: " + escape(bundle.channel.name) + " via Learning Equality Kolibri.</p>"
        (directory / (lesson.node_id + ".html")).write_text(_page(lesson.title, body), encoding="utf-8")
    for node, parent, title in bundle.topics:
        back = '<p><a href="' + parent + '.html">Back to topic</a></p>' if parent else ""
        body = back + "<ul>" + "".join(children[node]) + "</ul>"
        (directory / (node + ".html")).write_text(_page(title, body), encoding="utf-8")
    notice = ("<p>Selected offline materials from Learning Equality Kolibri. "
              "Interactive exercises and unsupported or unlicensed items are omitted. "
              "</p>")
    if publisher_notice:
        notice += "<p>" + escape(publisher_notice) + "</p>"
    (directory / "index.html").write_text(_page(bundle.channel.name, notice +
        "<ul>" + _link(bundle.channel.root_id, "Browse topics and lessons") + "</ul>"), encoding="utf-8")


def _illustration():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 48, 48, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x25\x64\x95" * 48) * 48))
            + chunk(b"IEND", b""))


def acquire(bundle, directory, *, progress=lambda n, total: None, guard=lambda n: None, publisher_notice=""):
    """Download only the selected media, validating exact upstream MD5 and size."""
    directory = Path(directory)
    (directory / "media").mkdir()
    count = 0
    for file in bundle.files:
        digest = hashlib.md5(usedforsecurity=False)
        size = 0
        with pdf_transport.open_pdf(STUDIO + "storage/" + file.storage_path, format_name="zim",
                                    limit=file.size, total_seconds=3600) as stream:
            with (directory / _media_name(file)).open("xb") as out:
                while block := stream.read(1024 * 1024):
                    guard(len(block))
                    size += len(block)
                    if size > file.size:
                        raise scope.ScopeError("Publisher media exceeds selected size.")
                    digest.update(block)
                    out.write(block)
                    progress(count + size, bundle.total_bytes)
        if size != file.size or digest.hexdigest() != file.checksum:
            raise scope.ScopeError("Publisher media checksum or size mismatch.")
        count += size
    render(bundle, directory, publisher_notice=publisher_notice)


def build_zim(bundle, directory, output, *, guard=lambda n: None, language="eng"):
    """Index the static pages with the existing Kiwix-compatible format."""
    directory, output = Path(directory), Path(output)
    if not re.fullmatch(r"[a-z]{3}", language):
        raise scope.ScopeError("Invalid ISO 639-3 language.")
    if output.exists():
        raise scope.ScopeError("Archive output already exists.")
    (directory / "illustration.png").write_bytes(_illustration())
    command = ["zimwriterfs", "--welcome=index.html", "--illustration=illustration.png",
               "--language=" + language, "--name=kolibri-" + bundle.channel.channel_id,
               "--title=" + bundle.channel.name[:30], "--description=Selected offline educational resources",
               "--creator=" + bundle.channel.name, "--publisher=Learning Equality / The Axiom",
               str(directory), str(output)]
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            while proc.poll() is None:
                guard(0)
                time.sleep(0.2)
            if proc.returncode:
                log.seek(max(0, log.tell() - 4096))
                diagnostic = log.read().decode("utf-8", errors="replace").strip()
                raise scope.ScopeError("ZIM conversion failed: " + diagnostic)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()



CONFIG_FIELDS = {"archive_generator", "generator_channel", "generator_scope", "generator_version",
                 "generator_metadata_sha256", "generator_transfer_bytes", "generator_language", "generator_notice"}
MAX_SELECTION = 15


def source_url(channel, version=None):
    suffix = "" if version is None else "-" + str(version)
    return STUDIO + "databases/" + scope._id(channel) + suffix + ".sqlite3"


def configuration(config):
    """Distinguish the generated artifact from the upstream database identity."""
    if config.get("archive_generator") != "kolibri" or config.get("format") != "zim":
        raise scope.ScopeError("Unsupported archive generator.")
    channel = scope._id(config.get("generator_channel"))
    nodes = config.get("generator_scope", "").split(",")
    if not 1 <= len(nodes) <= MAX_SELECTION or any(not scope.ID.fullmatch(n) for n in nodes):
        raise scope.ScopeError("Select educational topics before downloading.")
    version = config.get("generator_version", "")
    digest = config.get("generator_metadata_sha256", "")
    size = config.get("generator_transfer_bytes", "")
    language = config.get("generator_language", "")
    if (not re.fullmatch(r"(0|[1-9][0-9]{0,8})", version)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not re.fullmatch(r"[1-9][0-9]{0,12}", size)
            or not re.fullmatch(r"[a-z]{3}", language)
            or config.get("source_url") != source_url(channel, int(version))):
        raise scope.ScopeError("Review and save a versioned educational selection before downloading.")
    if any(config.get(key) for key in ("sha256", "publisher_sha1", "publisher_md5", "expected_size_bytes")):
        raise scope.ScopeError("Publisher database identity is not the generated ZIM checksum.")
    return channel, nodes, int(version), digest, int(size), language


def fields(bundle, nodes, digest, language):
    return {"archive_generator": "kolibri", "generator_channel": bundle.channel.channel_id,
            "generator_scope": ",".join(nodes), "generator_version": str(bundle.channel.version),
            "generator_metadata_sha256": digest, "generator_transfer_bytes": str(bundle.total_bytes),
            "generator_language": language, "source_url": source_url(bundle.channel.channel_id, bundle.channel.version),
            "expected_size_bytes": "", "sha256": "", "publisher_md5": "", "publisher_sha1": "",
            "edition": str(bundle.channel.version), "update_endpoint": source_url(bundle.channel.channel_id)}


def generate(config, directory, output, *, progress, guard):
    import shutil
    if shutil.which('zimwriterfs') is None:
        raise scope.ScopeError('Install the zim-tools package (zimwriterfs) before acquiring educational media.')
    channel, nodes, version, digest, size, language = configuration(config)
    directory = Path(directory)
    database = directory / "channel.sqlite3"
    guard(MAX_METADATA)
    fetch_metadata(channel, database, version=version, digest=digest)
    selected = select(database, channel, nodes, allow_full=True)
    if selected.total_bytes != size:
        raise scope.ScopeError("Selected transfer size changed.")
    pages = directory / "pages"
    pages.mkdir()
    guard(size * 2 + MAX_METADATA)
    acquire(selected, pages, progress=progress, guard=guard, publisher_notice=config.get("generator_notice", ""))
    build_zim(selected, pages, output, guard=guard, language=language)
