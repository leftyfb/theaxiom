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

The backend defaults to `127.0.0.1:8080` and serves the admin UI at `http://127.0.0.1:8080/admin`. Acquisition actions currently fail closed while storage safety integration is under development. This repository does not yet provide a complete installation package or a preloaded content library; local access is not a production deployment.

### Standard Nginx hosting

Nginx serves the public library homepage without credentials and proxies the
administration page and API to the unprivileged loopback-only Python backend.
See [standard deployment instructions](deploy/README.md) for the Nginx site,
systemd unit, initial credential setup and instance configuration. The admin
password is stored as a salted PBKDF2-HMAC-SHA256 hash and can be changed in the
administration interface without restarting services. HTTP development hosting
does not encrypt credentials; use a trusted isolated LAN and HTTPS for production.
The local loopback development server remains available for tests without credentials
when no Nginx mode is configured. Real acquisitions remain disabled.

### Storage configuration (development)

Open `/admin` and select Storage settings. Enter an existing storage root, select local or required mounted storage, and enter the expected mount target/source when applicable. Save and validate displays the saved configuration and its detected filesystem, free space, advisory write readiness and managed paths. Reload saved settings discards unsaved form edits. Saving unavailable storage is allowed for later recovery; readiness remains false and no data is relocated.

GET and POST /api/storage-settings read and save the complete configuration object. The default is unconfigured, with empty storage_root, storage_mount and storage_source, minimum_free_bytes set to zero, and fixed directories ["staging", "library"]. Select an existing absolute non-root storage_root. For mounted storage, specify both its exact mount target and expected source; leave both empty for intentionally local storage. Managed destinations are staging/ and library/zim/ under the root. Configuration is saved atomically under XDG_CONFIG_HOME/theaxiom/storage.json (or THEAXIOM_STORAGE_SETTINGS). Older or corrupt configuration must be explicitly replaced, not automatically activated.

GET /api/storage-status reports advisory readiness, detected mount, capacity and blocking errors. It makes no directories or probe writes. It checks the Linux mount table before inspecting filesystem paths, rejects symlinks and nested mounts, and checks available capacity against the selected source size. Hard NFS mounts can still block filesystem metadata operations. The checks cannot protect a subsequent writer from mount disappearance, replacement, or concurrent changes. Acquisition is therefore disabled, including when readiness passes. Existing staging environment overrides do not authorize acquisition. Persistent jobs, verified-local freshness, safe promotion and recovery remain outstanding.

Node.js is required for the JavaScript tests in the full development suite.

## License

A project license has not yet been selected. Until a license is added, do not assume that the project may be redistributed or reused under open-source terms. Third-party library content may have separate rights and license terms.
