# Excluded deployment tests

The source branch contains tests for Ansible roles, nginx, systemd, host inventory, and Vancouver-specific deployment wiring. Those deployment assertions remain excluded from this standalone application repository. Application-level behavior has been migrated into `tests/` with deployment assumptions removed or replaced by local fixtures. The source suite aggregator was replaced by `scripts/test_poc_suite.py`, which runs the destination's local pytest suite and provides a self-check. The Ansible-inventory preflight utility (`live_poc_preflight.py`) is excluded as deployment infrastructure. See `docs/architecture.md` for the boundary.

The following source tests remain excluded because they validate deployment infrastructure or a host lifecycle rather than standalone application behavior:

- `test_kiwix_artifact_gate.py`
- `test_kiwix_defaults.py`
- `test_kiwix_directories.py`
- `test_kiwix_fixture_stage.py`
- `test_kiwix_package_install.py`
- `test_kiwix_platform_preflight.py`
- `test_kiwix_service_management.py`
- `test_kiwix_systemd_unit.py`
- `test_live_poc_preflight.py`
- `test_poc_boot_persistence.py`
- `test_poc_ci_workflow.py`
- `test_poc_e2e.py`
- `test_poc_fixture_serving.py`
- `test_poc_health_checksum.py`
- `test_poc_listener_scope.py`
- `test_poc_no_acquisition.py`
- `test_poc_operations_docs.py`
- `test_poc_post_start_health.py`
- `test_poc_recovery_artifacts.py`
- `test_poc_report_ansible_idempotency.py`
- `test_poc_report_summary_deploy.py`
- `test_poc_rollback.py`
- `test_poc_rollback_preview.py`
- `test_poc_rollback_reports.py`
- `test_poc_service_ordering.py`
- `test_poc_web_config.py`
- `test_poc_web_config_preservation.py`
- `test_poc_web_config_transaction.py`
- `test_vancouver_poc_config.py`
- `test_poc_fixture_headers.py`
- `test_poc_service_version.py`

Some tests above combine application and deployment assertions in the source; their standalone-relevant behavior is covered by the migrated tests where applicable, while assertions requiring the excluded infrastructure remain out of scope.
