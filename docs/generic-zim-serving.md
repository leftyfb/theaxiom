# Generic ZIM serving and publication acceptance

## Current implementation (reviewed 2026-11-22)

The unified Kiwix reader is deployed on Vancouver, with the shared `/zim/` route and default loopback port 8081. The application prepares a private `kiwix-library.xml` manifest containing only explicitly published and independently verified ZIM archives. The reader configuration and deployment examples are in `deploy/` and `templates/`.

There is **no pending 8082 sidecar migration** and no standing, blanket authorization to deploy. The previously documented migration and request-time verification blockers are historical, not current live deployment holds. Confirm the actual deployed application SHA before relying on newer code paths.

## Publication and reader safety

- Download completion alone never publishes content. A verified archive, saved source identity, appropriate rights basis, and explicit publication intent are required.
- A Kiwix publication change writes its private manifest atomically; reader readiness is acknowledged only when its OPDS book set matches the expected set. Missing or stale runtime acknowledgement fails closed.
- Request-time `/zim/` authorization checks the acknowledged manifest, current publication intent, source identity and filename-derived content IDs. It **does not rehash archives or call the acquisition monitor on each request**. A revoked publication is denied even while a Kiwix reload is pending.
- The application checks installed ZIMs for verified identity, path and header safety when preparing its manifest. It does not install, start or restart Kiwix itself. Candidate updates must not displace the installed serving version before explicit verified activation.
- `/zim/viewer` provides library navigation. Deep links, published/unpublished state, search, assets and multi-book loading are covered by isolated regression checks, which do not replace real-server acceptance when changes warrant it.

The manifest currently uses Kiwix legacy library XML. Verify compatibility with the installed Kiwix/libkiwix version before any binary upgrade; do not assume an untested new manifest format is supported.

## CI regression

GitHub Actions runs unit/API/browser tests and isolated real Kiwix and Nginx smoke tests. CI-generated small ZIM fixtures exercise reader integration without touching Vancouver or production archives. Acquisition tests exercise persisted range validators, safe pause/resume/cancel, mismatch and failure behavior, installed archive preservation, and publication checks. Existing operator-selected storage directory modes (including 0777) must remain unchanged.

## Conditional live acceptance for relevant reader/acquisition changes

1. Verify explicit authorization for the exact CI-passing revision and compare it with the **observed** deployed revision. Preserve all existing archives, partials, catalog, rights flags and publication state.
2. Confirm current mount/source and running acquisition status; do not interfere with an active transfer. Check installed Kiwix binary compatibility with the existing manifest before migrating binaries or service settings.
3. If Nginx/systemd changes are in scope, validate the exact approved config diffs, `nginx -t`, backups and rollback. Do not bypass local safety gates.
4. Exercise Wikipedia navigation, search and deep links plus the other currently published readers. For reader-affecting changes, use a separate small test ZIM to verify publication, revocation, direct links and reload/acknowledgement behavior.
5. For acquisition-affecting changes, test with a **new small disposable transfer**, not installed large archives. Verify resumed range behavior, safe staging retention, checksum validation, and no implicit deletion.
6. Capture observed deployment SHA and results; treat unsuccessful live checks as failures rather than assuming CI success proves deployment success.

For current operator deployment status see [deployment acceptance register](deployment-acceptance-register.md). Safe replacement and explicit deletion work remains tracked in [#7](https://github.com/leftyfb/theaxiom/issues/7); this page does not declare unfinished remote-update detection complete.

### Offline map sources

The starter catalog includes two independent OpenStreetMap-derived packages:
StreetZim's contiguous United States ZIM (48 states plus DC, excluding Alaska
and Hawaii), and Protomaps' complete planet PMTiles vector basemap (native zoom
0–15; higher display zoom uses existing vector geometry). Both are opt-in
manual acquisitions, excluded from Axiom global search. StreetZim retains its
own in-reader address/place search and routing. PMTiles has no routing, address
search, satellite imagery, or terrain; its MapLibre renderer and style are
served locally, with system fonts and text labels rather than external sprites.
The included PMTiles style targets the Protomaps v4 vector schema.

The Internet Archive item metadata and Protomaps dated build index support
manual and periodic metadata-only update checks. Staged replacement, explicit
activation and rollback reuse the durable archive job workflow. StreetZim's
publisher SHA-1 and Protomaps' publisher MD5 are checked during byte
verification; both retain a measured local SHA-256 identity. These publisher
checksums are transport integrity evidence, not cryptographic signatures.
Protomaps also publishes BLAKE3 in its build metadata; the current runtime uses
MD5 to avoid a new platform dependency. StreetZim's satellite layer has a
noncommercial license (CC BY-NC-SA 4.0); retain upstream attribution and rights.

PMTiles content is held in `library/pmtiles`; the existing archive staging
and job directories are shared with ZIM jobs. The reader only exposes bounded
HTTP ranges for an installed, verified, published archive. No external CDN or
remote tile server is used at read time. The official packages are intentionally
not downloaded automatically; existing content and publication intent are
preserved. The two current releases require approximately 163 GiB installed,
plus staging space for any replacement and the configured free-space reserve.
