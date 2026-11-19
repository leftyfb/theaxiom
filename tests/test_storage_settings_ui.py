"""Exercise the real preferences JavaScript with a deterministic DOM/API fixture."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from jinja2 import Environment

TEMPLATE = Path(__file__).parents[1] / "templates/admin-page.html.j2"


def test_settings_markup():
    template = Environment().from_string(TEMPLATE.read_text())
    page = template.render(admin_page="settings", offline_library_content_manifest=[], offline_library_kiwix_zim_enabled=False)
    for key in ("storage-root", "storage-mode", "storage-mount", "storage-source",
                "storage-feedback", "status-free", "status-library"):
        assert f'id="{key}"' in page
    assert "Save and validate" in page
    assert 'href="/admin/settings" aria-current="page"' in page


def run_ui(scenario):
    script = re.search(r'<script id="storage-settings-script">(.*?)</script>',
                       TEMPLATE.read_text(), re.S).group(1)
    harness = r"""
const scenario = SCENARIO;
const elements = {};
const requests = [];
global.document = {getElementById: (id) => elements[id] ??= {
  value: '', textContent: '', disabled: false, hidden: false,
  addEventListener: function(event, fn) { this[event] = fn; }
}};
const defaults = {storage_root: '', storage_mount: '', storage_source: '',
  minimum_free_bytes: 0, directories: ['staging','library']};
let saved = scenario === 'reload' ? {...defaults,storage_root:'/existing'} : defaults;
global.fetch = async (url, options) => {
  requests.push({url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null});
  if (url === '/api/storage-settings' && options.method === 'POST') {
    if (scenario === 'invalid') return {ok:false,status:400,json:async()=>({error:'invalid storage settings'})};
    saved = JSON.parse(options.body);
  }
  if (url === '/api/storage-settings') return {ok:true,json:async()=>saved};
  if (url === '/api/storage-status') return {ok:true,json:async()=>({
    ready: scenario !== 'missing' && !!saved.storage_root, root:saved.storage_root,
    error: scenario === 'missing' ? 'required_mount_missing_or_replaced' : 'storage_not_configured',
    mount:{target:'/media/test',type:'nfs4',source:'server:/export'},
    write_ready: true,total_bytes:1099511627776,free_bytes:536870912000,
    paths:{staging:saved.storage_root+'/staging',library:saved.storage_root+'/library/zim'}
  })};
  throw Error(url);
};
const tick = () => new Promise(resolve => setImmediate(resolve));
"""
    harness = harness.replace("SCENARIO", json.dumps(scenario))
    harness += script + r"""
(async () => {
  await tick();
  elements['storage-root'].value = '/media/test';
  elements['storage-mode'].value = 'mounted';
  elements['storage-mode'].change();
  elements['storage-mount'].value = '/media/test';
  elements['storage-source'].value = 'server:/export';
  if (scenario === 'reload') await elements['storage-refresh'].click();
  else await elements['storage-form'].submit({preventDefault(){}});
  console.log(JSON.stringify({requests, feedback:elements['storage-feedback'].textContent,
    ready:elements['status-ready'].textContent, free:elements['status-free'].textContent,
    root:elements['storage-root'].value, library:elements['status-library'].textContent}));
})();
"""
    node = shutil.which("node")
    assert node, "Node.js required"
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_mounted_save_and_status():
    result = run_ui("ready")
    post = next(r for r in result["requests"] if r["method"] == "POST")
    assert post["body"] == {"storage_root": "/media/test", "storage_mount": "/media/test",
                            "storage_source": "server:/export", "minimum_free_bytes": 0,
                            "directories": ["staging", "library"]}
    assert result["ready"] == "Ready (advisory)"
    assert result["free"] == "500.00 GiB"
    assert result["library"] == "/media/test/library/zim"


def test_unavailable_mount_saved_but_not_ready():
    result = run_ui("missing")
    assert "Preferences saved, but storage is not ready" in result["feedback"]
    assert "missing or has been replaced" in result["ready"]


def test_invalid_save_reports_failure():
    result = run_ui("invalid")
    assert "Save failed: invalid storage settings" in result["feedback"]


def test_reload_restores_existing_saved_root():
    result = run_ui("reload")
    assert result["root"] == "/existing"
    assert not any(r["method"] == "POST" for r in result["requests"])
