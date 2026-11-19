"""Exercise polling against a deterministic DOM and API fixture."""
import json
import re
import shutil
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def run_ui(samples):
    script = re.search(r'<script id="acquisition-monitor-script">(.*?)</script>',
                       TEMPLATE.read_text(), re.S).group(1)
    harness = r"""
const samples = SAMPLES;
const fields = {};
const card = {dataset:{sourceId:'wikipedia-en'}, querySelector(selector) {
  const key = selector.split('"')[1];
  return fields[key] ??= {hidden:false, value:0, textContent:'', className:'', removeAttribute(name){delete this[name];}};
}};
const details = {open:true};
const requests = [];
let next;
global.document = {querySelectorAll: () => [card]};
global.window = {setTimeout(fn, ms) {if(ms===2000) next=fn; return 1;}, clearTimeout(){}};
global.fetch = async (url, options) => {
  requests.push({url, method:options.method || 'GET'});
  const sample = samples.shift();
  return {ok:sample!==null,json:async()=>sample};
};
""" .replace("SAMPLES", json.dumps(samples))
    harness += script + r"""
(async()=>{
  const tick = () => new Promise(resolve => setImmediate(resolve));
  await tick();
  const states = [JSON.parse(JSON.stringify(fields))];
  while(samples.length){ await next(); await tick(); states.push(JSON.parse(JSON.stringify(fields))); }
  console.log(JSON.stringify({states,requests,open:details.open}));
})();
"""
    node = shutil.which("node")
    assert node
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def job(state, **kwargs):
    return {"stale": False, "acquisitions": [dict(source_id="wikipedia-en", state=state,
        expected_bytes=100, downloaded_bytes=100, verified=False, **kwargs)]}


def test_live_states_and_read_only_polling():
    downloading = job("downloading", speed_bytes_per_second=10, eta_seconds=5)
    downloading["acquisitions"][0]["downloaded_bytes"] = 50
    complete = job("completed")
    complete["acquisitions"][0]["verified"] = True
    stale = dict(complete, stale=True)
    result = run_ui([downloading, job("verifying"), job("publishing"), complete, stale, None])
    states = result["states"]
    assert states[0]["transfer"]["value"] == 50
    assert "remaining" in states[0]["rate"]["textContent"]
    assert not states[1]["verification"]["hidden"]
    assert not states[2]["verification"]["hidden"]
    assert "100.00%" in states[1]["metrics"]["textContent"]
    assert states[3]["verified"]["textContent"] == "Confirmed by acquisition service"
    assert states[4]["verified"]["textContent"] == "Not confirmed"
    assert states[4]["verification"]["hidden"]
    assert states[5]["status"]["textContent"] == "Status unavailable"
    assert all(r == {"url": "/api/acquisitions", "method": "GET"} for r in result["requests"])


def test_unrelated_job_not_displayed():
    sample = job("completed")
    sample["acquisitions"][0]["source_id"] = "unrelated"
    state = run_ui([sample])["states"][0]
    assert state["status"]["textContent"] == "Waiting for status"
    assert state["transfer"]["hidden"]

def test_checksum_measured_pass_and_fallback():
    first = job("verifying", checksum_bytes=81, checksum_pass=1, checksum_passes=2)
    second = job("verifying", checksum_bytes=12, checksum_pass=2, checksum_passes=2)
    states = run_ui([first, second, job("verifying"), dict(second, stale=True)])["states"]
    assert states[0]["verification"]["value"] == 81
    assert "pass 1 of 2" in states[0]["verification-label"]["textContent"]
    assert "81.0%" in states[0]["verification-label"]["textContent"]
    assert states[1]["verification"]["value"] == 12
    assert "pass 2 of 2" in states[1]["verification-label"]["textContent"]
    assert "value" not in states[2]["verification"]
    assert states[3]["verification"]["hidden"]
