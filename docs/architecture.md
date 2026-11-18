# Architecture

## Scope

The Axiom is a standalone Python application and content-preparation toolkit for serving a curated offline reference library. This repository contains the application code, HTML/YAML templates, release and verification scripts, schemas, fixtures, and tests migrated from earlier offline-library development.

The repository contains standard Nginx, systemd and public web-root examples under `deploy/`. Host inventory, Ansible roles, secrets and Projectwright integration remain external. Nginx is the standard frontend; the Python backend owns administrative authentication and dynamic API functionality.

## Runtime boundaries

- `src/theaxiom/`: importable Python application modules and POC command implementations.
- `templates/`: application-rendered catalog, document, homepage, manifest, and acquisition-plan templates. Deployment templates were not migrated.
- `scripts/`: operator/developer utilities that operate on explicit paths and local HTTP endpoints.
- `fixtures/`: small deterministic content and captured POC artifacts used for offline tests.
- `schemas/`: JSON schemas for generated evidence, status, and update reports.
- `tests/`: standalone-compatible tests. Tests that only verified Ansible/nginx/systemd/Vancouver wiring remain listed in `tests/EXCLUDED_DEPLOYMENT_TESTS.md` rather than being made to pass against a different architecture.

## Supported host baseline

The application targets Ubuntu 24.04 LTS and Ubuntu 26.04 LTS on supported Python versions (Python 3.10+). The application itself is user-space Python, with standalone loopback development support. Standard LAN hosting uses Nginx and systemd; operators select host-specific listening addresses, storage paths, and Kiwix installation as content serving is implemented.

## Data flow

1. Curated source metadata is represented in manifests and catalog templates.
2. Acquisition remains explicitly gated and checksum-driven; the migrated daemon and scripts preserve incomplete early-stage behavior without enabling bulk transfer by default.
3. Release/report helpers produce JSON artifacts validated against `schemas/`.
4. A local HTTP service can serve the generated catalog and fixture content; `offline_verification.py` validates the serving contract without requiring WAN access.
5. Freshness probing is read-only and accepts an injected opener in tests.

## Configuration and paths

No Ansible variable namespace is part of the standalone runtime contract. Catalog metadata lives in `config/catalog.yaml`. The acquisition daemon reads `$XDG_CONFIG_HOME/theaxiom/acquisition.yaml` (or `THEAXIOM_MANIFEST`); its data, cache, and state default under the corresponding XDG user directories. `THEAXIOM_STAGING_DIR`, `THEAXIOM_SOURCES_DIR`, `THEAXIOM_QUARANTINE_DIR`, and `THEAXIOM_STATUS_PATH` can override those locations. The admin API also supports `THEAXIOM_ACQUISITION_SCRIPT`. `config/acquisition.example.yaml` is an inert template with bulk acquisition disabled and no items. Repository fixtures are used only by tests. Standard route and process supervision examples are in `deploy/`; host-specific service identities, mount definitions and secrets remain operator-controlled.

## Preserved incomplete work

Acquisition, the admin API, live verification, freshness probing, report/evidence generation, catalog rendering, and release-gate tooling are retained as early-development components. They are not represented as production-ready interfaces merely because they are packaged.
