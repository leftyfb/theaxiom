# The Axiom

The Axiom is a self-hosted project for preparing and serving a curated offline reference library over a local network.

It is intended for households, community groups, schools, makerspaces, and other organizations that want useful reference material available when Internet access is unavailable. A locally hosted library can be browsed from nearby phones, tablets, and computers using hardware and storage managed by the operator.

The Axiom is software for building and operating a library, not a preloaded collection of books, archives, or media. Operators choose the content, verify its source and redistribution rights, provide suitable hardware and storage, and decide how the library is made available on their local network.

## Intended workflow

1. **Curate content.** Select reference materials and record where they came from, their licensing terms, and other relevant metadata.
2. **Prepare a release.** Stage selected materials, verify their integrity, and organize them into a library that is ready to publish.
3. **Serve locally.** Make the prepared library available through a local web interface so users on the network can browse it without relying on the public Internet.

Preparation and serving can be performed on one machine or separated across multiple systems, depending on the installation. The project is designed to be adaptable to different hardware and operating environments.

## Content and safety

The project does not grant rights to third-party content. Before including or redistributing any resource, verify its license, attribution requirements, and any restrictions on use. Preserve source and version information so operators can identify what is in their library and where it came from.

Content quality matters, especially for emergency, health, water, sanitation, and other safety-related topics. Operators are responsible for choosing trustworthy sources and reviewing the materials they publish. The Axiom is a way to make selected resources available offline; it is not a substitute for professional advice or official emergency guidance.

## Project status

The Axiom is in early development. Its goals include reliable preparation and local serving of curated reference materials, with clear provenance and explicit operator control over what is published. It is not yet a production-ready appliance or a finished, universally supported installation package.

This repository is the standalone home for the project. The current migration preserves the application and proof-of-concept tooling while intentionally excluding Ansible, host inventory and Projectwright integration; standard nginx/systemd deployment examples now live in `deploy/`. See `docs/architecture.md` and `docs/backlog.md` for boundaries and next work.

**Deployment is operator-authorized and gated by CI and deployment safety checks.** The latest confirmed Vancouver deployment at the time of this documentation update is `8d1f9b506698275c4849abd6f5a186f19d2cc561` ([AC-166](https://github.com/leftyfb/agent-coordination/issues/706)). A green GitHub Actions run alone does not authorize deployment.

## License

The Axiom application source code and original project documentation are licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

This license **does not apply** to downloaded, imported, or served ZIM, PDF, EPUB, Wikipedia, Ready.gov, or other third-party content. Each resource retains its own copyright and license; operators are responsible for permission, attribution, and redistribution terms. External dependencies (including Kiwix, PDF.js, Python packages, and system tools) retain their respective licenses. No third-party material is relicensed by this repository's LICENSE. See the [licensing and provenance audit](docs/licensing.md) and [dependency documentation](docs/dependencies.md).

## Development

The supported development baseline is Ubuntu 24.04 LTS and Ubuntu 26.04 LTS with Python 3.10 or newer:

```shell
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The test suite is local and deterministic by default. Fixtures and generated reports are used to exercise the preserved POC behavior without requiring Ansible, nginx, systemd, Projectwright, or outbound network access.

To run the fixture-backed smoke checks and full test suite after installation:

```shell
.venv/bin/python -m pytest -q tests/test_offline_verification.py tests/test_live_poc_verify.py tests/test_live_poc_verify_json.py
.venv/bin/python scripts/test_poc_suite.py
```

For a local admin UI and API development check, start the backend and open the admin page in a browser. The API is also available at `/health`:

```shell
.venv/bin/python -m theaxiom.admin_api
curl http://127.0.0.1:8080/health
```

The backend defaults to `127.0.0.1:8080` and serves the admin UI at `http://127.0.0.1:8080/admin`. Legacy /api/action acquisitions remain fail-closed. The dedicated ZIM downloader supports download, pause, resume and cancel through revision-gated admin controls; see docs/generic-zim-serving.md for verification and publication safeguards. This repository does not yet provide a complete installation package or a preloaded content library; local access is not a production deployment.

### Standard Nginx hosting

Nginx serves the public library homepage without credentials and proxies the
administration page and API to the unprivileged loopback-only Python backend.
See [standard deployment instructions](deploy/README.md) for the Nginx site,
systemd unit, initial credential setup and instance configuration. The admin
password is stored as a salted PBKDF2-HMAC-SHA256 hash and can be changed in the
administration interface without restarting services. Interactive administration
uses a branded HTML sign-in and short-lived server-side sessions with same-origin
CSRF enforcement; explicit Basic Authorization remains available for API clients.
HTTP development hosting does not encrypt credentials or cookies; use a trusted
isolated LAN and HTTPS for production. Chrome/1Password save/fill needs live
browser acceptance before declaring #67 complete.
The local loopback development server remains available for tests without credentials
when no Nginx mode is configured. The legacy /api/action acquisition adapter remains disabled; dedicated ZIM and document acquisition/import flows have separate safeguards.

### Source profiles

For a step-by-step offline content guide, open **Administration → Sources → Content source guide**. The guide is bundled with the application; its upstream links require internet access. See also [adding content sources](docs/adding-sources.md).

Source configuration is stored separately from content at `${XDG_CONFIG_HOME:-~/.config}/theaxiom/sources.json`; set `THEAXIOM_SOURCE_CATALOG` to select an explicit file. Shipped proposals have an explicit `source_catalog.SEED_CATALOG_VERSION` and are copied only when the catalog is first created. They become ordinary editable and removable records; later software releases do not reapply modified defaults, resurrect deleted records, or download/publish content. Empty catalogs remain empty. Seed version changes require a separate operator-approved migration; there is no automatic reconciliation. The catalog is versioned, revision-checked, atomically replaced, and created with owner-only permissions. A malformed, oversized, unknown-version, or unreadable catalog fails closed rather than being silently reset. Back up this file with configuration; it does not contain or move downloaded content.

`/admin/sources` uses the same card and add/edit form for every persisted record, including initial seed records. An empty catalog shows no source cards. `GET /api/sources` returns the revision and records with `source_id`, `profile` (display metadata), and `configuration` (format and candidate metadata). Separate, nullable `artifact`, `acquisition`, and `publication` fields describe the artifact, managed acquisition status, and reader controls; `mutation_blocked` indicates active or unresolved acquisition. `POST /api/sources` returns the same view and accepts exactly `operation`, `source_id`, `fields`, and `revision`. Use `add`, `edit`, or `remove`, with profile and configuration edits supplied together in `fields`. Mutations require same-origin authenticated requests and the current revision; conflicting revisions return HTTP 409.

A source profile records its format, candidate URL, expected byte count and SHA-256 when available, provenance, and license. The API reports `adapter` and `configuration_valid` separately from `artifact` and `installed_verified`. Wikipedia, Ready.gov, and other ZIMs use the same managed download, verification, and publication workflow. Once published, a ZIM opens through `/read/zim/<source-id>` and the shared Kiwix reader. Use the current link on the library homepage to update old bookmarks.

The admin download controls use format-specific actions against the saved source profile and current catalog revision. `POST /api/publication` requires `source_id`, `enabled`, and the current catalog `revision`; enabling requires matching verified content and a ready reader.

Unpublish before removing a published source or changing its artifact identity. Removal deletes only the catalog record; it never moves or deletes installed bytes. Re-adding equivalent configuration reconnects matching acquisition status but does not republish automatically. Active or unresolved acquisition blocks catalog mutations regardless of profile origin.

### Import a trusted PDF for offline reading

1. In Sources, add a `pdf` source with a plain HTTPS publisher URL, provenance and redistribution basis, exact byte count (at most 16 MiB), and SHA-256. An administrator must independently review the origin, redistribution rights, and document safety. These fields are attestations; the app does not contact the publisher or establish that a PDF is safe to render.
2. Use an explicitly configured local storage root or NFS mount with matching `storage_mount` and `storage_source` (no nested mounts). An operator must provision `staging/pdf` and `library/pdf` within it, owned by the application user and not group/world-writable. Place the trusted local file at `staging/pdf/<source-id>.pdf` with the required file ownership and permissions. Managed directory modes, including 0777, are an operator-controlled policy; files remain subject to owner, mount, symlink and write-safety checks. Symlinks, hard links and special files are refused.
3. Select **Import local PDF** in that source's card. The app checks the storage reserve, streams and verifies the document framing and pin, and atomically installs the copy under `library/pdf/<source-id>.<sha256>.pdf`. Failure leaves previously installed bytes in place. The original inbox file is not deleted. Older pins are retained, not automatically pruned.
4. Publish separately from the same card. The library links directly to the integrated offline PDF reader; see [PDF.js installation and reader limits](docs/pdfjs.md). Unpublishing blocks new reader and direct document requests; it cannot revoke previously saved copies. PDF full-text search is not included.

PDF HTTPS acquisition and safe ZIM transport have separate workers and validation rules. The shared Kiwix reader and `/zim/viewer` are deployed on Vancouver; source publication still requires verified installed content and explicit operator action. No production metadata is registered from the historical demo fixture.

In Admin → Sources, **Notify only** is the default update policy and preserves the source's existing periodic-check preference. **Disabled** skips periodic checks but leaves manual actions available. **Automatic** and **Scheduled** opt into downloading, verifying, and activating replacements for an already installed, verified ZIM or PMTiles archive, using current publisher metadata. PDF and EPUB do not support unattended replacement. Scheduled windows use server-local time, may cross midnight, and restrict when a transfer starts, not when it finishes. Settings can pause new unattended starts without changing the metadata-check schedule. Activation preserves publication intent and the previous archive. A failed or cancelled digest is not automatically attempted again; review and retry it manually from Sources.

Rolling back a successful automatic or scheduled update suppresses that edition. On the source card under **Technical details**, use **Allow automatic update again** to authorize one retry when the current publisher check matches the rolled-back edition. This does not start a download or change publication settings. Once eligible (including pause and window checks), the worker can reuse the retained verified archive. Manually staged replacements still require manual review.

### Storage configuration (development)

Open `/admin` and select Storage settings. Enter an existing storage root, select local or required mounted storage, and enter the expected mount target/source when applicable. Save and validate displays the saved configuration and its detected filesystem, free space, advisory write readiness and managed paths. Reload saved settings discards unsaved form edits. Saving unavailable storage is allowed for later recovery; readiness remains false and no data is relocated.

GET and POST /api/storage-settings read and save the complete configuration object. The default is unconfigured, with empty storage_root, storage_mount and storage_source, minimum_free_bytes set to zero, and fixed directories ["staging", "library"]. Select an existing absolute non-root storage_root. For mounted storage, specify both its exact mount target and expected source; leave both empty for intentionally local storage. Managed destinations are staging/ and library/zim/ under the root. Configuration is saved atomically under XDG_CONFIG_HOME/theaxiom/storage.json (or THEAXIOM_STORAGE_SETTINGS). Older or corrupt configuration must be explicitly replaced, not automatically activated.

GET /api/storage-status reports advisory readiness, detected mount, capacity and blocking errors. It makes no directories or probe writes. It checks the Linux mount table before inspecting filesystem paths, rejects symlinks and nested mounts, and checks available capacity against the selected source size. Hard NFS mounts can still block filesystem metadata operations. The checks cannot protect a subsequent writer from mount disappearance, replacement, or concurrent changes. Storage readiness alone does not authorize acquisition; dedicated acquisition workers apply additional source-specific validation, verification, and recovery safeguards. Existing staging environment overrides do not authorize acquisition.

For generic ZIM serving, published status requires an explicitly verified installed
archive, any necessary rights acknowledgement, a matching Kiwix manifest and an
acknowledged running reader. The deployed shared Kiwix reader uses port 8081 and serves verified archives through the `/zim/` routes. Application publishing actions do not install or start the Kiwix service.
See [generic ZIM acceptance and deployment gate](docs/generic-zim-serving.md).

Node.js is required for the JavaScript tests in the full development suite.
