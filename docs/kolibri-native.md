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

## Optional runtime provisioning

The application can be deployed without installing Kolibri. In this state the
preconfigured educational card remains available to the administrator but no
reader or downloads are enabled. The optional service has a separate local
home, native learner database and credential; it uses the existing
`theaxiom` service identity to permit the authenticated Axiom job coordinator
to run the official native disk import commands without sudo.

After exact-revision CI and guarded application deployment, an independently
authorized local operator can run:

```sh
sudo bash /opt/theaxiom/app/scripts/install_kolibri_optional.sh EXACT_DEPLOYED_SHA
```

The installer refuses existing native configuration, occupied ports, missing
TLS material or unresolved acquisitions. It pins Kolibri 0.19.5 in a separate
virtualenv, uses the configured library mount for live content and retained
verified packages, and stores native application state privately. It creates
a separate native administrator and leaves the bootstrap record readable
only by root. No channel metadata or educational files are acquired and no
channel is published. The reader is closed on both origins until Axiom has
acknowledged an exact native publication.

The main proxy is optional. HTTP uses a separate alternate origin on 8084,
HTTPS on 8443; loopback-only native endpoints are 8082 and 8083. The two
public alternate listeners use the same host as the browser, with the
existing certificate for TLS. The pinned vendor settings compatibility
module restricts browser CSP to those alternate ports. On HTML only, the
Nginx scheme map changes the vendor's compact alternate port setting;
the existing CA and TLS files are not rewritten.

The installer is not part of the ordinary guarded deployment runner, which
does not install new packages, services or public listener configurations.
Extend that local owner-controlled deployment authorization separately
before unattended production activation; never route this action through
a development agent or broaden deployment permissions from an issue comment.

## Lifecycle and search

The source profile contains a generic Kolibri channel ID. The authenticated
scope picker fetches official metadata only, allows hierarchical selection,
and estimates exact deduplicated content bytes and license inventory before
starting a transfer. The version-pinned package retains its manifest digest
in the private job ledger and is imported via native Kolibri disk commands.
The retained package is distinct from the live native content store; allow
additional storage for the native copy and retained rollback package.

Both Nginx reader origins use a fail-closed acknowledgement registry.
An in-flight or interrupted native publication transaction closes both
origins, and direct native database export or remote-source selectors are
not exposed. The verified post-import leaf set must match the reviewed
selection, including all previously published channels. The local public
search adapter reads only available nodes from this acknowledged live
database, searches topic/lesson titles and descriptions, and returns
native permalink URLs; caption files are native local WebVTT resources.
Transcript full-text indexing is not enabled in this initial adapter; native
caption and transcript display is retained. The official Khan Academy website
favicon identifies the source, not Axiom itself, and is accompanied by the
publisher free-availability attribution and unofficial-access description.

Updates compare official channel versions and preserve the exact installed
selection; a removed or changed node that cannot be resolved into the new
version is reported for review rather than silently expanding scope.
The existing metadata sweep and batch update controls use installed
manifest-bound evidence. Native update promotion and rollback retain the
same learner database and replace only channel content. Native publication
and rollback require explicit verified state; a failed transaction deliberately
keeps the reader closed pending explicit operator recovery from the verified
previously acknowledged packages. The saved scope can also be queued through
Axiom’s shared initial batch download interface.


The official version-4 Khan Academy US metadata URL returned HTTP 404 in
validation; the publisher does not currently expose that prior version at
the standard versioned endpoint. Multi-version promotion/rollback is
therefore covered with deterministic versioned fixture packages, while the
live official probe exercises removal and restoration of the verified
available version. No unsupported historical metadata is synthesized.

### Acquisition throughput and recovery (#151)

Acquisitions use up to eight workers, with at most eight queued resource
operations and an acquisition-local HTTPS connection pool. Each request and
redirect still validates the complete current DNS answer and uses the pinned
public address and TLS hostname. Only fully consumed, unambiguously framed
responses are eligible for reuse. Partial responses and failed requests close
their connection.

Short network reads are accumulated before a guarded storage write (1 MiB or
0.5 seconds, whichever comes first). Each worker checks storage before writing,
with capacity headroom for concurrent writes. Cancellation is checked between
reads and during retry delays. Resource progress counts checksummed complete
files only: retrying a failed file does not advance the byte count, and
resumed verified bytes do not inflate the newly transferred speed.

The private job ledger binds the staging token to the exact reviewed selection.
Resume verifies the metadata SHA-256 and each retained resource's publisher
checksum and size, retrieves missing/invalid resources, and rebuilds the
deterministic manifest. No partial package is published. Interrupted updates
remain separate from the installed version; explicit deletion discards partial
files. Storage/source identity cannot be changed while a partial exists.

Controlled local benchmark (32 x 256 KiB synthetic resources, 15 ms request
latency, 1 ms per 16 KiB short read, 2 ms per storage guard):

| Workers | Elapsed | Throughput |
| ---: | ---: | ---: |
| 1 | 1.322 s | 6.05 MiB/s |
| 4 | 0.340 s | 23.55 MiB/s |
| 8 | 0.181 s | 44.30 MiB/s |

The guard regression also demonstrates two guarded writes for 128 individual
16 KiB reads. These figures isolate simulated latency and are not a publisher
or NFS benchmark. The development environment could not resolve the upstream
through direct DNS or access the Vancouver library mount. Measure real
publisher throughput and NFS latency independently on Vancouver after the
active transfer completes, before accepting the performance improvement.
