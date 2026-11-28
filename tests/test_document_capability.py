from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO

import pytest

from theaxiom import document_capability as cap, document_origin_evidence, pdf_transport
from theaxiom import storage_settings, update_discovery_config

URL = "https://publisher.org/manual.pdf"
DATA = b"verified installed content"
TAG = '"stable"'
PIN = {"source_url": URL, "bytes": len(DATA), "sha256": sha256(DATA).hexdigest()}


def record(kind="pdf", **fields):
    return {"source_id": "guide", "configuration": {"format": kind, "source_url": URL,
             "sha256": PIN["sha256"], **fields}}


def response(**fields):
    return {"source_url": URL, "reachable": True, "status": 200, "etag": TAG, **fields}


@pytest.mark.parametrize("kind", ["pdf", "epub"])
def test_verified_get_and_head_comparable(kind):
    outcome = cap.inspect(record(kind), installed_etag=TAG, pin=PIN, probe=lambda *a, **k: response())
    assert outcome["state"] == "supported"
    assert outcome["recommended_policy"] == "notify"
    assert "does not prove future editions" in outcome["evidence"]
    pending = cap.inspect(record(kind), probe=lambda *a, **k: response())
    assert pending["state"] == "pending" and not pending["recommended_policy"]


@pytest.mark.parametrize("metadata,reason,policy", [
    ({"reachable": False, "status": 403}, "access_denied", ""),
    ({"reachable": False, "status": 503}, "temporary_or_unavailable", ""),
    ({"reachable": False, "status": 405}, "unsupported_method", "disabled"),
    ({"etag": "", "validator_kind": "weak"}, "weak_validator", "disabled"),
    ({"etag": ""}, "missing_validator", "disabled"),
    ({"source_url": "https://publisher.org/other.pdf"}, "changed_url", "disabled"),
])
def test_capability_classes(metadata, reason, policy):
    outcome = cap.inspect(record(), installed_etag=TAG, pin=PIN,
                          probe=lambda *a, **k: response(**metadata))
    assert outcome["reason"] == reason
    assert outcome["recommended_policy"] == policy


def test_local_and_separate_endpoint_do_not_probe():
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported URL must not be probed")
    assert cap.inspect(record(source_url=""), probe=forbidden)["reason"] == "local_only"
    assert cap.inspect(record(update_endpoint="https://publisher.org/other.pdf"), probe=forbidden)["reason"] == "unsupported_url"


def test_conditional_304_with_installed_get_validator_is_comparable():
    outcome = cap.inspect(record(), installed_etag=TAG, pin=PIN,
                          probe=lambda *a, **k: response(status=304, etag=""))
    assert outcome["recommended_policy"] == "notify"


@pytest.mark.parametrize("data,expected", [(DATA, "baseline_verified"), (b"x"*len(DATA), "baseline_mismatch")])
def test_explicit_recheck_can_establish_matching_baseline_only(tmp_path, monkeypatch, data, expected):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    rec = record()
    @contextmanager
    def read(url, **kwargs):
        assert url == URL and kwargs["required_etag"] == TAG and kwargs["limit"] == len(DATA)
        stream = BytesIO(data)
        stream.etag = TAG
        yield stream
    monkeypatch.setattr(pdf_transport, "open_pdf", read)
    outcome = cap.inspect(rec, pin=PIN, establish=True, probe=lambda *a, **k: response())
    assert outcome["reason"] == expected
    assert document_origin_evidence.load("guide", "pdf", URL, PIN["sha256"]) == (TAG if expected == "baseline_verified" else None)


def test_capability_record_bound_to_url_and_installed_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_settings, "SETTINGS_PATH", tmp_path / "storage.json")
    rec = record()
    outcome = cap.inspect(rec, installed_etag=TAG, pin=PIN, probe=lambda *a, **k: response())
    cap.save(rec, outcome)
    assert cap.status(rec) == outcome
    assert cap.status(record(source_url="https://publisher.org/revised.pdf"))["state"] == "pending"
    assert cap.status(record(sha256="a"*64))["state"] == "pending"
    assert update_discovery_config.policy(record(update_policy="recommend")["configuration"])[0] == "disabled"
