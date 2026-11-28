"""One-time, bounded official Kolibri channel metadata compatibility probe."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from theaxiom import kolibri_scope as scope

CID = "c9d7f950ab6b5a1199e3d6c10d7f0103"
BASE = "https://studio.learningequality.org/content/"
URL = BASE + "databases/" + CID + ".sqlite3"

with tempfile.TemporaryDirectory(prefix="axiom-kolibri-probe-") as temp:
    path = Path(temp) / "channel.sqlite3"
    begun = time.monotonic()
    sha = hashlib.sha256()
    size = 0
    with urlopen(Request(URL, headers={"User-Agent": "TheAxiom metadata verification"}), timeout=30) as response, path.open("wb") as target:
        final = urlsplit(response.geturl())
        if final.scheme != "https":
            raise ValueError("Non-HTTPS distribution endpoint")
        print("Distribution host:", final.hostname, "Content-Length:", response.headers.get("Content-Length"), flush=True)
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > scope.MAX_DATABASE_BYTES or time.monotonic() - begun > 180:
                raise ValueError("Metadata limit exceeded")
            sha.update(block)
            target.write(block)
    print("Metadata bytes:", size, "SHA256:", sha.hexdigest(), flush=True)
    with sqlite3.connect(path.absolute().as_uri()+"?mode=ro&immutable=1", uri=True) as conn:
        for table in ("content_channelmetadata", "content_contentnode", "content_file", "content_localfile"):
            print(table, [row[1] for row in conn.execute("PRAGMA table_info("+table+")")], flush=True)
        print("Channel:", conn.execute("SELECT id,name,version,root_id FROM content_channelmetadata").fetchall(), flush=True)
        print("Kinds:", conn.execute("SELECT kind,count(*) FROM content_contentnode GROUP BY kind").fetchall(), flush=True)
        print("Licenses:", conn.execute("SELECT license_name,count(*) FROM content_contentnode WHERE kind!='topic' GROUP BY license_name").fetchall(), flush=True)
    channel = scope.inspect_channel(path, CID)
    print("Top-level topics:", flush=True)
    for topic in scope.children(path, CID):
        try:
            plan = scope.plan_scope(path, CID, [topic.node_id])
            print(json.dumps({"id":topic.node_id,"title":topic.title,"resources":plan.resource_count,"bytes":plan.total_bytes,"missing_license":plan.missing_license_count}), flush=True)
        except scope.ScopeError as exc:
            print(json.dumps({"id":topic.node_id,"title":topic.title,"error":str(exc)}), flush=True)
    all_scope = scope.plan_scope(path, CID, [channel.root_id], allow_full=True)
    print("Full channel estimate:", all_scope.total_bytes, "resources:", all_scope.resource_count, "unique files:", len(all_scope.files), flush=True)
    # No media payloads downloaded, no production or service changes.
