# Standard Nginx deployment (Ubuntu)

Nginx owns public HTTP delivery. The Python backend runs as an unprivileged,
loopback-only process; it owns admin authentication and password rotation.
The public homepage is static and requires no login. No Kiwix or acquisition
worker is configured in this initial deployment.

## Operator setup

Use a dedicated `theaxiom` service account and a root-owned application checkout
at `/opt/theaxiom/app` with a Python virtual environment installed from the
repository. The service account needs read access to the application and write
access only to its private application state (and eventually separately
authorized content storage). Do not change ownership of an existing NFS export
or create content directories during deployment.

1. Install Nginx and Python prerequisites using the host package manager. Install
   the application into `/opt/theaxiom/app/.venv` using `pip install -e .`.
2. Create `/var/lib/theaxiom` owned by the service account, mode 0700. From a
   trusted local terminal initialize its credential file (interactive, never
   put the password in a command line, Git, issue or log):
   `sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -m theaxiom.admin_security init /var/lib/theaxiom/admin-credential.json`.
   The command refuses to overwrite an existing credential.
3. Copy `server.env.example` to `/etc/theaxiom/server.env`, root-owned, and
   keep the loopback backend settings; no public host allowlist is needed. No passwords
   are kept in the environment. The credential file is a private salted hash.
4. Install `theaxiom.service` in `/etc/systemd/system/`, run
   `systemctl daemon-reload` and `systemctl enable --now theaxiom.service`.
   Verify only 127.0.0.1:8080 is listening.
5. Install the two Nginx files from `nginx/` as indicated by their comments.
   The site listens on all IPv4 and IPv6 interfaces on port 80, including
   LAN and Tailscale interfaces, and accepts any requested hostname or IP.
   Enable the site, resolve conflicting default/listening sites with operator
   approval, run `nginx -t`, then reload Nginx. Do not port-forward to the
   public Internet unless that is a deliberate operator decision.
6. Verify unauthenticated GET / returns the public homepage; /admin and
   /api/storage-settings return 401 without credentials; authenticated admin
   opens and settings work; acquisition POST returns 503; service units enabled.
   Verify password rotation using the admin form, including old credentials
   failing, new credentials working, and persistence across a backend restart.
   Do not put real credentials in test transcripts.

Password changes require the current password and confirmation and take effect
without Nginx or backend reload. For recovery stop the backend, preserve the
private existing hash under an operator-controlled backup name, initialize a
new credential as the service account, then restart. Never silently reset a
credential. Browser Basic authentication may cache credentials until the browser
session is closed. Basic authentication over HTTP is **not encrypted**; use this
HTTP example only on a trusted isolated development LAN. Configure Nginx HTTPS
with a trusted certificate before use on untrusted networks. The backend uses
the scheme forwarded by the configured local Nginx proxy when checking Origin.

The example IP and host names are deployment examples, not application defaults.
Ansible may consume these files from the application repository. Keep actual
host inventory, custom firewall rules and secrets outside Git.

## External acquisition monitoring

The admin page polls GET /api/acquisitions every two seconds. A single
background sampler queries the allowlisted acquire-wikipedia-zim.service
and file metadata, never reads archive content or controls the service.
The initial adapter is specific to the existing Wikipedia service at
/media/offline; other configurations report unavailable or unknown.
Completion requires successful current-invocation journal evidence, not
file size alone. After a reboot or journal rotation, verification evidence
may be unavailable; the UI must not claim verification. Rate samples are
in memory and rebuild after backend restart. A hard NFS hang may stall
the sampler; the endpoint still returns promptly with stale status.
The independent storage-readiness endpoint is unchanged.
Acquisition POST remains disabled.

Preserve any existing theaxiom UID, primary group, supplementary groups,
and local state ownership during updates. For NFS AUTH_SYS, configure
a dedicated server-side identity with appropriate share permissions and
match its numeric UID on the application host; check group requirements
and test access as the service account. Do not recursively chmod/chown
the share or map all clients to an administrator.


## Optional publisher update-check timer (not enabled automatically)

The checked-in `theaxiom-update-check.service` and `theaxiom-update-check.timer`
are **examples only**; deploying or enabling them requires a separately
authorized, exact-revision deployment and systemd configuration review. The
service runs `python -m theaxiom.kiwix_update_scheduler` as the existing
unprivileged application user, using the same private configuration as the
administration backend. It attempts at most 10 source-scoped metadata checks
per invocation. A daily 03:00 local-time timer has up to one hour of
randomized delay and is persistent across downtime.

This path performs only bounded publisher metadata requests and writes
private check-status JSON under application state. It **never downloads a ZIM
archive, activates a replacement, changes rights or publication, restarts
Kiwix, deletes content, or modifies NFS storage**. Old results are bound to
the exact installed SHA-256 and expire after 48 hours. Unavailable or
ambiguous metadata is reported as unknown rather than as an update. A manual
`Check for updates` button is available for verified generic ZIM sources.

The first iteration supports only versioned generic ZIMs in recognized
Kiwix naming families whose trusted installed-job identity and official
catalog metadata can be matched. It does not establish universal coverage
for Wikipedia's legacy acquisition, arbitrary PDF/EPUB sources, feeds
requiring pagination, or alternative Metalink publisher formats. Do not
enable automatic archive updates on the strength of these checks.

Before enabling the timer, review the deployed source-catalog location,
service account privileges, proxy restrictions, NFS availability, rate
limits, exact systemd unit content and existing acquisition state. No
existing running service or download must be interrupted.

### Update-check diagnostics

In **Admin → Sources**, expand a source's **Technical details** to see the
cached check timestamp, evidence, and available candidate identity. Scheduled
and manual checks emit one `source_update_check` JSON summary with a request
ID, source ID, phase, reason code, duration, timestamp, cache status, and
publisher host. HTTP status is included when available for the reported phase.
Failed checks use warning level. The event excludes publisher response bodies,
full URLs, validator values, exception messages, and credentials.

Read recent summaries in the systemd journal for the application and
`theaxiom-update-check.service`. Search for `source_update_check` and the source
ID. Summary retention follows the host's journal settings. A cache hit
includes the age of the original check and does not make another publisher
request. Checking metadata does not transfer or change installed files.
