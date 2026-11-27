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

## Acquisition monitoring

Managed ZIM acquisition status comes from the local job ledger. The admin
backend ships without a registered external acquisition unit.

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
administration backend. It checks every eligible installed source with periodic
checks enabled, with up to eight concurrent checks and a 60-second deadline per
source. The default daily 03:00 local-time timer has up to one hour of randomized
delay and is persistent across downtime.

Admin → Settings shows the server time zone and next run. Saving an interval
and preferred time overrides the default timer without changing systemd
configuration. The Admin backend runs the custom schedule and attempts the
latest missed slot after downtime. Scheduled and manual sweeps share a lock.

On Admin → Updates, **Check for all updates** checks every eligible installed
source, including sources with periodic checks disabled. The Sources navigation displays an available-update count; select **Updates**
from the administration menu to review them. Use status filters and search to
review installed and candidate editions, update policy, check timestamps,
maintenance windows, and space estimates. Select visible eligible updates,
then confirm **Download X updates** to queue replacements and **Publish X
updates** to activate verified candidates. The installed archives serve until
activation, and publication preferences remain in effect. Individual checks
and the global automatic-update pause use the existing settings and source
APIs; opening the page only reads status. Use **Source details** for individual
source actions and Settings for the schedule.

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
for arbitrary PDF/EPUB sources, feeds
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


### Source activity

Admin → Activity displays committed source changes newest first and can
filter by action and source, including removed sources. History starts at
installation of this release; it does not invent past events from the
current catalog or transient queue. The journal is a private SQLite file
`source-activity.sqlite3` next to the storage settings. Back it up with
the application configuration. No download URLs, configuration values,
credentials, or diagnostic messages are copied into events; source IDs,
display names, editions, and outcomes are retained.

Events have stable IDs and monotonically increasing sequence numbers.
Consumers may use sequence cursors and durable acknowledgement via the
journal's `acknowledge` method. Retention defaults to 10,000 acknowledged
events; unacknowledged records are retained until all registered consumers
have acknowledged them. The retain argument can be adjusted per consumer
maintenance invocation. No automatic deletion occurs without consumer
acknowledgement. Deduplication IDs are retained separately.


Active archive transfers interrupted by service restart automatically resume
from retained bytes and matching publisher validators. Downloads explicitly
paused or cancelled by the operator remain stopped. If the partial is missing,
the source configuration changed, or a safe validator is unavailable, review
the source status; recovery never silently discards retained content.
Queued automatic replacements retain their existing policy and maintenance
window restrictions.

Update policy is the only background discovery control. Disabled allows
explicit checks only; Notify only checks at the global cadence; Automatic
and Scheduled also apply eligible verified updates. Legacy periodic opt-outs
(including contradictory Automatic/Scheduled profiles) migrate to Disabled;
previous opt-ins retain their selected policy. Manual discovery never runs
periodic checks. Existing maintenance windows and archive bytes are retained.

Activity records update discovery and queue transitions independently from
completed activations. Repeated deferrals with the same reason are deduplicated
per job; only fixed, sanitized reason categories are retained.

### Completed-update notifications

The library and admin surfaces poll for successful activation events from
the private Activity journal. A notification is claimed for 30 seconds and
acknowledged only after a full five-second visible toast or explicit dismissal.
Delivery is once per Axiom instance (one visitor/tab, not once per account);
unacknowledged claims replay after the lease expires, including after a restart
or closing a tab before display finishes. Acknowledged events are not replayed;
historical successful activation events already in Activity are initially eligible.
Public visitors can receive only currently published, reader-ready source
notifications, using public display names. Admin-only events remain pending
for an authenticated admin. No URLs, errors, policy or other private activity
details are returned by the notification endpoint. Unacknowledged update
events are excluded from journal retention pruning.

The script-free EPUB chapter view retains its restrictive content security
policy; pending notices are delivered on the next eligible library/admin
navigation instead of enabling script execution in that reader.
