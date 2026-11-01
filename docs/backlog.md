# Prioritized backlog

This backlog records the migrated project's state; it does not start implementation work.

## P0 — verified defects

- **Admin UI has no standalone serving contract.** The UI's action/job API usage is not connected to a documented server entry point; the prior deployment proxy only covered health. Add integration tests and an explicit local server contract before considering the UI usable.

## P1 — incomplete functionality

- Acquisition still needs operator-facing validation of configuration, catalog-to-manifest mapping, and safe promotion/rollback. It remains disabled by default; configurable XDG paths do not make the workflow production-ready.
- Freshness and catalog metadata are not an operator-approved update policy; source probes report status but do not enforce licensing review or release approval.
- Provide a supported standalone CLI for catalog rendering, acquisition planning, report generation, status, and verification.
- Define a portable storage layout and safe promotion/rollback workflow for verified content.
- Add installation/test jobs on Ubuntu 24.04 and Ubuntu 26.04, including optional Kiwix integration.
- Replace deployment-specific operator procedures in the retained historical role documentation with standalone instructions where applicable.

## P2 — proposed enhancements

- Add a persistent metadata store and content provenance UI.
- Add pluggable HTTP/process supervision adapters rather than assuming nginx or systemd.
- Add signed release manifests and independent verification keys.
- Add accessibility, localization, and responsive UI acceptance tests.
- Add reproducible release archives and a supported upgrade/migration format.

## Explicit non-goals for this migration

- No Ansible role or playbook migration.
- No nginx or systemd deployment implementation.
- No Vancouver host integration.
- No Projectwright integration.
- No backlog implementation beyond the migration and standalone test harness.
