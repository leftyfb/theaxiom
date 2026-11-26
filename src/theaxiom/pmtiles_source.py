"""PMTiles v3 vector archive envelope checks and publication projection."""
import re
import struct
from . import publication, zim_jobs

ROUTE = re.compile(r"/api/library/pmtiles/([a-z0-9-]{1,63})(/map\.pmtiles)?\Z")


def check_header(header, size):
    """Validate the v3 vector header and all file section bounds."""
    if len(header) != 127 or header[:8] != b"PMTiles\x03":
        raise ValueError("Invalid PMTiles v3 header")
    root, root_len, metadata, metadata_len, leaves, leaves_len, tiles, tiles_len = struct.unpack_from("<8Q", header, 8)
    sections = sorted((start, start + length) for start, length in ((root, root_len), (metadata, metadata_len), (leaves, leaves_len), (tiles, tiles_len)) if length)
    if (not 127 <= root < size or not root_len or not tiles_len
            or any(start < 127 or end > size for start, end in sections)
            or any(previous[1] > following[0] for previous, following in zip(sections, sections[1:]))
            or header[99] != 1
            or header[97] not in (1, 2) or header[98] not in (1, 2)
            or not 0 <= header[100] <= header[101] <= 30):
        raise ValueError("Invalid PMTiles section bounds or vector tile type")


def status(record):
    sid = record["source_id"]
    path = "/api/library/pmtiles/" + sid
    result = dict(enabled=False, verified=False, serving=False, published=False,
                  path=path, entry_path=path, publication_reason="")
    try:
        enabled = publication.load().get(sid, False)
        job = zim_jobs.status(sid)
        verified = bool(job and job.get("verified") is True and job.get("active") is False
                        and job["configuration"].get("format") == "pmtiles"
                        and zim_jobs.installed(job, job["configuration"]))
        rights = bool(record["configuration"].get("license", "").strip())
        ready = enabled and verified and rights
        return dict(result, enabled=enabled, verified=verified, serving=ready, published=ready,
                    ready_for_publication=verified, requires_rights_acknowledgement=verified and not rights)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(result, error="PMTiles publication state unavailable")
