#!/usr/bin/env bash
# Explicit operator-side provisioning. Never invoked by an HTTP endpoint,
# ordinary deployment, or acquisition worker. No educational content import.
set -euo pipefail
umask 077

app=/opt/theaxiom/app
state=/var/lib/theaxiom/kolibri
venv=/opt/kolibri/venv
unit=/etc/systemd/system/kolibri.service
dropin=/etc/systemd/system/theaxiom.service.d/kolibri.conf
timerdropin=/etc/systemd/system/theaxiom-update-check.service.d/kolibri.conf
snippet=/etc/nginx/snippets/theaxiom-kolibri-main.conf
servers=/etc/nginx/conf.d/theaxiom-kolibri.conf
bootstrap=/var/lib/theaxiom/kolibri-admin-bootstrap.json

[[ $# == 1 && $1 =~ ^[a-f0-9]{40}$ && $EUID == 0 ]] || {
  echo "Usage: sudo install_kolibri_optional.sh EXACT_DEPLOYED_REVISION" >&2; exit 2;
}
[[ $(git -C "$app" rev-parse HEAD) == "$1" ]] || { echo "Unexpected deployed revision" >&2; exit 1; }
for target in "$state" "$venv" "$unit" "$dropin" "$timerdropin" "$snippet" "$servers" "$bootstrap"; do
  [[ ! -e $target && ! -L $target ]] || { echo "Existing Kolibri installation: $target" >&2; exit 1; }
done
[[ -r /etc/theaxiom/tls/server.pem && -r /etc/theaxiom/tls/server.key ]] || {
  echo "Existing Axiom TLS certificate and key required; no certificate generated." >&2; exit 1;
}
if ss -H -ltn '( sport = :8082 or sport = :8083 or sport = :8084 or sport = :8443 )' | grep -q .; then
  echo "Required native ports are occupied" >&2; exit 1
fi
nginx -t

# Refuse to restart Axiom while a recorded acquisition is active/unresolved.
check_transfers() {
  runuser -u theaxiom -- env THEAXIOM_STORAGE_SETTINGS=/var/lib/theaxiom/storage.json \
    "$app/.venv/bin/python" - <<'PY'
from pathlib import Path
from theaxiom import acquisition_queue, catalog_store, storage_settings, zim_jobs
catalog = catalog_store.CatalogStore(storage_settings.SETTINGS_PATH.with_name("sources.json")).get(initialize=False)
if any(job["state"] in acquisition_queue.PENDING for job in acquisition_queue.status()):
    raise SystemExit("Existing queued or active acquisition; defer installation.")
for record in catalog["sources"]:
    job = zim_jobs.read(record["source_id"])
    if job and job.get("active"):
        raise SystemExit("Existing active acquisition; defer installation.")
PY
}
check_transfers

# Validate configured mount and create only the native-format leaf and live
# content directory as the existing service account; do not chmod shared roots.
runuser -u theaxiom -- env THEAXIOM_STORAGE_SETTINGS=/var/lib/theaxiom/storage.json \
  "$app/.venv/bin/python" - <<'PY'
import os
from theaxiom import pdf_source
with pdf_source._directory("library", writing=True, format_name="kolibri") as (fd, mount, minimum):
    os.mkdir("live", mode=0o700, dir_fd=fd)
PY
install -d -m 0700 -o theaxiom -g theaxiom "$state" "$state/home" "$state/registry"
python3 -m venv "$venv"
"$venv/bin/python" -m pip install 'kolibri==0.19.5'
"$venv/bin/python" -c 'from importlib.metadata import version; assert version("kolibri") == "0.19.5"'

# Generate an independent native admin password; never place it on a command
# line or in the journal. Keep a root-only recovery record, not Axiom credentials.
env THEAXIOM_STORAGE_SETTINGS=/var/lib/theaxiom/storage.json "$app/.venv/bin/python" - <<'PY'
import configparser, json, os, secrets
from pathlib import Path
from pwd import getpwnam
from theaxiom import storage_settings
account = getpwnam("theaxiom")
home = Path("/var/lib/theaxiom/kolibri/home")
content = Path(storage_settings.load_settings()["storage_root"])/"library"/"kolibri"/"live"
config = configparser.ConfigParser()
config.optionxform = str
config.read("/opt/theaxiom/app/deploy/kolibri/options.ini.example")
config["Paths"]["CONTENT_DIR"] = str(content)
config["Paths"]["AUTOMATIC_PROVISION_FILE"] = str(home/"provision.json")
with (home/"options.ini").open("x") as output:
    config.write(output)
password = secrets.token_urlsafe(32)
provision = {
    "facility_name": "Axiom offline learning", "preset": "informal",
    "device_settings": {"language_id": "en", "landing_page": "learn", "allow_guest_access": True,
                        "allow_peer_unlisted_channel_import": False, "allow_learner_unassigned_resource_access": True},
    "superuser": {"username": "kolibri_admin", "password": password},
}
with (home/"provision.json").open("x") as output:
    json.dump(provision, output)
with Path("/var/lib/theaxiom/kolibri-admin-bootstrap.json").open("x") as output:
    json.dump(provision["superuser"], output)
for path in (home/"options.ini", home/"provision.json"):
    os.chown(path, account.pw_uid, account.pw_gid)
PY

# All subsequent paths are new, so rollback only removes those added configs.
rollback() {
  local result=$?
  trap - ERR
  systemctl disable --now kolibri.service >/dev/null 2>&1 || true
  rm -f -- "$unit" "$dropin" "$timerdropin" "$snippet" "$servers"
  systemctl daemon-reload || true
  if nginx -t; then systemctl reload nginx || true; fi
  echo "Kolibri activation failed. New private state and package retained for owner review." >&2
  exit "$result"
}
trap rollback ERR
install -d -m 0755 "$(dirname "$dropin")" "$(dirname "$timerdropin")"
install -m 0644 "$app/deploy/kolibri/kolibri.service.example" "$unit"
install -m 0644 "$app/deploy/kolibri/axiom-runtime.conf.example" "$dropin"
install -m 0644 "$app/deploy/kolibri/axiom-runtime.conf.example" "$timerdropin"
ln -s "$app/deploy/kolibri/main-proxy.conf.example" "$snippet"
ln -s "$app/deploy/kolibri/alternate-servers.conf.example" "$servers"
nginx -t
systemctl daemon-reload
systemctl enable --now kolibri.service
for attempt in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8082/kolibri/ >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8082/kolibri/ >/dev/null
runuser -u theaxiom -- "$app/.venv/bin/python" - <<'PY'
import sqlite3
db = "/var/lib/theaxiom/kolibri/home/db.sqlite3"
with sqlite3.connect("file:"+db+"?mode=ro", uri=True) as conn:
    assert conn.execute("SELECT count(*) FROM content_channelmetadata").fetchone() == (0,)
PY
check_transfers
systemctl restart theaxiom.service
systemctl reload nginx
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8080/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8080/health >/dev/null
[[ $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/kolibri/) == 403 ]]
[[ $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8084/content/static/sandbox/) == 403 ]]
trap - ERR
echo "Native Kolibri service installed; both reader origins are closed until explicit publication."
