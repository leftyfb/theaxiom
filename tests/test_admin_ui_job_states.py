"""Exercise polling against a deterministic DOM and API fixture."""
import json
import re
import shutil
import subprocess

from test_admin_page_template import render


def run_ui(samples):
    script = re.search(r'<script id="acquisition-monitor-script">(.*?)</script>', render(), re.S).group(1)
    harness = r"""
const samples = SAMPLES;
const fields = {};
const card = {setAttribute(){}, querySelectorAll(){return [];}, dataset:{sourceId:'wikipedia-en'}, querySelector(selector) {
  const key = selector.split('"')[1] || selector;
  return fields[key] ??= {dataset:{}, hidden:false, value:0, textContent:'', className:'', removeAttribute(name){delete this[name];}};
}};
const requests = [];
let next;
global.document = {activeElement:{closest(){return null;}},
  querySelectorAll: selector => selector === '.source-row' ? [] : [card], getElementById: id =>
  ['source-update-summary', 'source-refresh-feedback'].includes(id) ? (fields[id] ??= {}) : ({})};
global.window = {axiomSourceRevision: () => 0, setTimeout(fn, ms) {if(ms===2000) next=fn; return 1;}, clearTimeout(){}};
global.fetch = async (url, options) => {
  requests.push({url, method:options.method || 'GET'});
  const sample = samples.shift();
  return {ok:sample!==null,json:async()=>sample};
};
""".replace("SAMPLES", json.dumps(samples))
    harness += script + r"""
(async()=>{
  const tick = () => new Promise(resolve => setImmediate(resolve));
  await tick();
  const states = [JSON.parse(JSON.stringify(fields))];
  while(samples.length){ await next(); await tick(); states.push(JSON.parse(JSON.stringify(fields))); }
  console.log(JSON.stringify({states,requests}));
})();
"""
    node = shutil.which("node")
    assert node
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def job(state, **kwargs):
    return {"revision": 0, "acquisition_stale": False, "sources": [dict(
        source_id="wikipedia-en", adapter="kiwix", artifact={"bytes": 100}, mutation_blocked=False,
        acquisition=dict(state=state, expected_bytes=100, downloaded_bytes=100,
                         active=state in {"downloading", "verifying", "publishing"}, verified=False, **kwargs))]}


def test_live_states_and_read_only_polling():
    downloading = job("downloading", speed_bytes_per_second=10, eta_seconds=5)
    downloading["sources"][0]["acquisition"]["downloaded_bytes"] = 50
    complete = job("completed")
    complete["sources"][0]["acquisition"]["verified"] = True
    stale = dict(complete, acquisition_stale=True)
    result = run_ui([downloading, None, job("verifying"), job("publishing"), complete, stale])
    states = result["states"]
    failure = states.pop(1)
    assert not failure["source-refresh-feedback"]["hidden"]
    assert failure["summary-progress"] == states[0]["summary-progress"]
    assert failure["status"] == states[0]["status"]
    assert states[1]["source-refresh-feedback"]["hidden"]
    assert states[0]["transfer"]["value"] == 50
    assert states[0]["summary-progress"]["value"] == 50
    assert not states[0]["summary-progress"]["hidden"]
    assert "remaining" in states[0]["rate"]["textContent"]
    assert "50.0% · 50 B / 100 B" in states[0]["transfer-summary"]["textContent"]
    assert "10 B/s" in states[0]["transfer-summary"]["textContent"]
    assert "remaining" in states[0]["transfer-summary"]["textContent"]
    assert states[3]["transfer-summary"]["hidden"]
    assert not states[1]["verification"]["hidden"]
    assert not states[2]["verification"]["hidden"]
    assert "100.0%" in states[1]["metrics"]["textContent"]
    assert states[3]["transfer"]["hidden"]
    assert states[3]["summary-progress"]["hidden"]
    assert states[4]["summary-progress"]["hidden"]
    assert states[4]["verification"]["hidden"]
    assert all(r == {"url": "/api/sources", "method": "GET"} for r in result["requests"])


def test_installed_zim_sizes_use_verified_artifact_bytes():
    adopted = job("completed")
    adopted_source = adopted["sources"][0]
    adopted_source.update(zim_download=True, installed_verified=True, artifact={"bytes": 109_500_000_000})
    adopted_source["acquisition"].pop("expected_bytes")
    adopted_source["acquisition"]["verified"] = True
    acquired = job("completed")
    acquired_source = acquired["sources"][0]
    acquired_source.update(zim_download=True, installed_verified=True, artifact={"bytes": 3_420_000_000})
    acquired_source["acquisition"]["verified"] = True
    unknown_size = dict(adopted, sources=[dict(adopted_source, artifact={})])
    states = run_ui([adopted, acquired, unknown_size])["states"]
    assert [state["status"]["textContent"] for state in states] == [
        "109.5 GB", "3.42 GB", "Installed · size unavailable"
    ]
    assert all(state["status"]["className"] == "badge enabled" for state in states)


def test_no_matching_job_waits_for_status():
    sample = job("completed")
    sample["sources"][0]["acquisition"] = None
    state = run_ui([sample])["states"][0]
    assert state["status"]["textContent"] == "Waiting for status"
    assert state["transfer"]["hidden"]


def test_checksum_measured_pass_and_fallback():
    first = job("verifying", checksum_bytes=81, checksum_pass=1, checksum_passes=2)
    second = job("verifying", checksum_bytes=12, checksum_pass=2, checksum_passes=2)
    states = run_ui([first, second, job("verifying"), dict(second, acquisition_stale=True)])["states"]
    assert states[0]["verification"]["value"] == 81
    assert "pass 1 of 2" in states[0]["verification-label"]["textContent"]
    assert "81.0%" in states[0]["verification-label"]["textContent"]
    assert states[1]["verification"]["value"] == 12
    assert "pass 2 of 2" in states[1]["verification-label"]["textContent"]
    assert "value" not in states[2]["verification"]
    assert states[3]["verification"]["hidden"]


def test_replacement_checksum_percent_is_separate_from_transfer_completion():
    sample = job("completed")
    sample["sources"][0]["replacement"] = dict(state="verifying", downloaded_bytes=100,
                                               expected_bytes=100, checksum_bytes=24,
                                               checksum_pass=2, checksum_passes=2)
    state = run_ui([sample])["states"][0]["[data-zim-replacement-status]"]["textContent"]
    assert "pass 2 of 2" in state
    assert "24.0% read (digest not yet confirmed)" in state
    assert "100.0%" not in state


def test_admin_update_summary_survives_poll_failure_and_refreshes_on_recovery():
    sample = job("completed")
    sample["update_summary"] = "1 source has an update available"
    first, failure, recovered = run_ui([sample, None, job("completed")])["states"]
    assert first["source-update-summary"]["textContent"] == sample["update_summary"]
    assert not first["source-update-summary"]["hidden"]
    assert failure["source-update-summary"] == first["source-update-summary"]
    assert not failure["source-refresh-feedback"]["hidden"]
    assert recovered["source-refresh-feedback"]["hidden"]
    assert recovered["source-update-summary"]["hidden"]


def test_publication_refresh_updates_summary_and_controls():
    sample = job("completed")
    sample["sources"][0]["publication"] = {"enabled": True, "serving": True, "published": True}
    success, failure = run_ui([sample, None])["states"]
    assert success["publication-summary"]["textContent"] == "Published"
    assert success["publication-status"]["textContent"] == "Published · available in library"
    assert success["publication-toggle"]["textContent"] == "Unpublish"
    assert not success["publication-toggle"]["disabled"]
    assert not success["publication-browse"]["hidden"]
    assert not success["[data-catalog-remove]"]["disabled"]
    assert "mutation-note" not in success
    assert "mutation-note" not in failure
    for field in ("[data-catalog-remove]", "publication-summary", "publication-toggle", "publication-browse"):
        assert failure[field] == success[field]


def test_planned_source_keeps_unsupported_label_during_polling():
    sample = job("not_started")
    sample["sources"][0].update(adapter=None, artifact=None, acquisition=None)
    state = run_ui([sample])["states"][0]
    assert state["status"]["textContent"] == "Not yet supported"


def test_update_available_badge_only_when_cached_evidence_confirms_it():
    confirmed = job("completed")
    confirmed["sources"][0]["installed_verified"] = True
    confirmed["sources"][0]["artifact"] = {"bytes": 1048576}
    confirmed["sources"][0]["update_check"] = {
        "state": "update_available", "candidate_edition": "2026-11",
        "candidate_sha256": "b" * 64,
        "candidate_bytes": 123456,
        "candidate_url": "https://download.kiwix.org/zim/wikipedia_2026-11.zim",
    }
    unconfirmed = job("completed")
    unconfirmed["sources"][0]["update_check"] = {
        "state": "unable_to_determine",
    }
    states = run_ui([confirmed, unconfirmed, job("completed")])["states"]
    assert states[0]["update-status"]["hidden"] is False
    assert states[0]["[data-review-zim-update]"]["hidden"] is False
    assert states[0]["[data-review-zim-update]"]["dataset"]["updateBytes"] == "123456"
    assert not states[0]["update-details"]["hidden"]
    assert "2026-11" in states[0]["update-details"]["textContent"]
    assert "Estimated transfer and staging: 123.46 kB" in states[0]["update-details"]["textContent"]
    assert "Installed and candidate archives together: 1.17 MB" in states[0]["update-details"]["textContent"]
    assert states[1]["update-status"]["hidden"] is True
    assert states[1]["[data-review-zim-update]"]["hidden"] is True
    assert states[1]["update-details"]["hidden"] is True
    assert states[1]["update-details"]["textContent"] == ""
    assert states[2]["update-status"]["hidden"] is True


def test_document_origin_status_is_informational_not_an_update_badge():
    changed = job("completed")
    changed["sources"][0].update(
        adapter="pdf", update_check={"state": "representation_changed"})
    unchanged = job("completed")
    unchanged["sources"][0].update(
        adapter="pdf", update_check={"state": "representation_unchanged"})
    inconclusive = job("completed")
    inconclusive["sources"][0].update(
        adapter="pdf", update_check={"state": "unable_to_determine", "evidence": "No saved ETag"})
    states = run_ui([changed, unchanged, inconclusive])["states"]
    assert states[0]["discovery-evidence"]["textContent"] == "Publisher file changed · newer edition unverified"
    assert states[0]["update-status"]["hidden"] is True
    assert states[1]["discovery-evidence"]["textContent"] == "No change detected"
    assert states[2]["discovery-evidence"]["textContent"] == "Couldn’t compare with the installed file"
    assert states[2]["update-evidence"]["textContent"] == "No saved ETag"


def test_replacement_transfer_updates_without_reload_and_preserves_installed_size():
    def sample(state, **fields):
        data = job("completed")
        data["sources"][0].update(installed_verified=True, artifact={"bytes": 140_000_000_000},
                                  replacement=dict(state=state, **fields))
        return data

    states = run_ui([
        sample("downloading", downloaded_bytes=50, expected_bytes=200, speed_bytes_per_second=10, eta_seconds=15),
        sample("downloading", downloaded_bytes=100, expected_bytes=200),
        sample("retrying", downloaded_bytes=100, expected_bytes=200, speed_bytes_per_second=10),
        sample("verifying", downloaded_bytes=200, expected_bytes=200, checksum_bytes=40, checksum_pass=1, checksum_passes=1),
        sample("verified", downloaded_bytes=200, expected_bytes=200),
        sample("failed"),
        sample("downloading", downloaded_bytes=20),
    ])["states"]
    assert all(s["status"]["textContent"] == "140 GB" for s in states)
    assert states[0]["replacement-summary-progress"]["value"] == 25
    assert states[0]["replacement-progress"]["value"] == 25
    assert "50 B / 200 B" in states[0]["transfer-summary"]["textContent"]
    assert "10 B/s" in states[0]["transfer-summary"]["textContent"]
    assert "remaining" in states[0]["transfer-summary"]["textContent"]
    assert states[1]["replacement-summary-progress"]["value"] == 50
    assert "B/s" not in states[1]["transfer-summary"]["textContent"]
    assert "B/s" not in states[2]["transfer-summary"]["textContent"]
    assert states[3]["replacement-progress"]["value"] == 20
    assert "Checksum" in states[3]["transfer-summary"]["textContent"]
    for index in (4, 5):
        assert states[index]["replacement-summary-progress"]["hidden"]
        assert states[index]["replacement-progress"]["hidden"]
    assert "total unknown" in states[6]["transfer-summary"]["textContent"]
    assert "value" not in states[6]["replacement-progress"]


def test_initial_transfer_summary_matches_replacement_metrics_and_clears_rate():
    initial = job("downloading", speed_bytes_per_second=25, eta_seconds=6)
    initial["sources"][0]["acquisition"].update(downloaded_bytes=50, expected_bytes=200)
    replacement = job("completed")
    replacement["sources"][0].update(installed_verified=True, replacement=dict(
        initial["sources"][0]["acquisition"]))
    retry = job("downloading")
    retry["sources"][0]["acquisition"].update(state="retrying", downloaded_bytes=50, expected_bytes=200,
                                              speed_bytes_per_second=25, eta_seconds=6)
    stale = dict(initial, acquisition_stale=True)
    states = run_ui([initial, replacement, retry, stale, job("completed")])["states"]
    first, second, third, fourth, fifth = states
    assert first["transfer-summary"]["textContent"].replace("Downloading ·", "Downloading update ·") == second["transfer-summary"]["textContent"]
    assert "25 B/s" not in third["transfer-summary"]["textContent"]
    assert "remaining" not in third["transfer-summary"]["textContent"]
    assert fourth["transfer-summary"]["hidden"]
    assert fifth["transfer-summary"]["hidden"]
