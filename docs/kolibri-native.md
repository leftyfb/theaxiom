# Native Kolibri integration (#34)

Direction approved by the operator on 2026-11-28: retain native Kolibri as an
optional local service rather than converting educational content to ZIM.
The prototype branch does not enable a production service or publish content.

## Local service and routes

Pin Kolibri 0.19.5 for the initial compatibility test. The example service uses
a dedicated Kolibri home and separate virtualenv. Select free loopback ports
(the examples use 8082 for the main app and 8083 for sandboxed content).
Use `URL_PATH_PREFIX=/kolibri/` and preserve the full URI in Nginx.
`/kolibri` redirects to the slash-terminated prefix. Inject the relative
`Back to library` link only into HTML, without rewriting scripts, APIs, captions
or range responses. Keep this accessible on desktop and mobile and through
ordinary LAN, hostname and Tailscale access without hardcoded client URLs.

Kolibri's upstream source explicitly recommends a separate browser origin
for packaged interactive content. The example uses a configurable additional
TLS port and a separate loopback upstream with credentials stripped. Do not
collapse this origin into the main Axiom server, forward Axiom credentials,
or loosen existing CSRF/authentication rules. The separate port must use
the existing trusted local certificate and be validated against real content.
Ports are origin boundaries, not cookie boundaries. Preserve HttpOnly session
cookies and audit any readable cookies and cross-origin requests. A production
proxy must enforce publication/availability and return safely when optional
Kolibri is not installed. These examples are deliberately not included in
the production Nginx site yet.

Kolibri 0.19.5 has a numeric ZIP_CONTENT_ORIGIN CSP type bug (HTTP 500).
The isolated settings override in deploy/kolibri/axiom_kolibri_settings.py
preserves upstream CSP and converts only numeric proxy ports to valid,
port-restricted HTTP(S) sources. This is narrower than upstream's default
scheme-wide frame policy and does not modify the installed vendor package.
Review compatibility when upgrading Kolibri.

## Content package and publication

Use native Kolibri channel databases and content-addressed media files. The
`kolibri_native` staging helper downloads a version-pinned database and the
reviewed selection (including native exercise packages), verifies file byte
counts and upstream MD5 identifiers, records local SHA-256 digests, and writes
a completed manifest last. The package manifest digest must be retained in
Axiom's trusted job ledger. A verified directory is compatible with upstream
`importchannel disk` and `importcontent --node_ids ... disk`; no converter,
archive repacking or untrusted HTML parsing is required.

The scope planner exposes the upstream license inventory. Official channel
availability and the presence of license metadata do not alone establish
redistribution permission; respect publisher terms and review exceptions.
Axiom's acquisition lifecycle must own private staging, progress/ETA, cancel,
mount/capacity guards, retry, and integrity verification before import. The
staging helper expects an exclusively managed directory and does not change
library or staging share permissions.

Native Kolibri import is not itself a transactional Axiom publication
operation. Axiom must keep staged files out of the public runtime, serialize
native publication/import/removal, gate the reader while a transaction runs,
preserve learner accounts and progress, and verify exact installed channel
versions and availability. Unpublishing must make the resources inaccessible
through the native interface/API/media paths, not just hide an Axiom card.
Evaluate native channel removal and reimport or a content database snapshot
without reverting learner databases. Keep a verified prior package for rollback
and re-use content-addressed files where possible without violating publication.
A native administrator is a trusted operator, not a substitute for Axiom's
publication gate. No production enabling until those boundaries are tested.

Global search should use a generic local adapter limited to published available
nodes, with stable native lesson links. Add transcript indexing from local
captions if upstream search does not index them. Preserve explicit scope and
publisher edition across updates; never implicitly expand scope. Use the native
guest/learner workflow rather than proxying or sharing Axiom administrator
credentials.

## Verification

The isolated CI proof imports only one small official Khan Academy video
with captions and one small exercise, never the whole channel. Test an actual
Nginx TLS proxy at a mobile viewport with nonlocal browser requests blocked;
verify navigation back to the library, video playback and Perseus exercise
rendering. Further acceptance tests must include answering an exercise,
subtitle display, alternate-origin packaged HTML, global search,
publication/removal and rollback. No production download or install has
been authorized by the probe.
