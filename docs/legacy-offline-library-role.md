# Historical Ansible offline-library role reference

> Historical context only. This document describes the source Ansible role and the Vancouver deployment that existed before The Axiom was separated into this repository. It is not a supported standalone installation or operations guide; referenced Ansible variables, paths, services, and commands are not provided here.

The base role installs dependencies and creates the service account and empty
local directories. Content acquisition is disabled by default.

## External storage preflight

Future acquisition tasks must be guarded by `offline_library_acquisition_enabled`.
When enabled, the role requires all of these variables:

```yaml
offline_library_acquisition_enabled: true
offline_library_storage_source: "//storage.example/library"
offline_library_storage_mount: /mnt/offline-library
offline_library_storage_minimum_free_bytes: 322122547200
offline_library_storage_directories:
  - sources
  - releases
  - quarantine
  - reports
```

The configured destination must be an existing, non-root mount whose reported
device exactly matches `offline_library_storage_source`, and its available bytes
must meet the configured threshold. The role only checks mount facts: it does
not mount storage, contact a storage server, or download content.

After the preflight passes, the role creates the configured relative storage
directories as the dedicated service account. Directory entries must be simple
relative names; absolute paths and parent-directory traversal are rejected.

## Content manifest and acquisition gate

`offline_library_content_manifest` is a sanitized planning list. Each item must
contain exactly `id`, `title`, `kind`, `source_url`, `sha256`, `destination`, and
`enabled`. URLs must use HTTPS, checksums must be explicit SHA-256 values, and
destinations must be traversal-safe paths relative to the storage mount. The
`id`, `source_url`, and `destination` values must each be unique across the
manifest; duplicate source URLs are rejected. The role renders the validated
list to `/etc/offline-library/content-manifest.yaml`.

The manifest is not a downloader. This role performs no network request or
content transfer, and `offline_library_bulk_acquisition_approved` must remain
`false`. The future acquisition workflow is: select and review manifest entries,
obtain explicit operator approval, rerun the exact mount/free-space preflight,
download into `quarantine`, verify SHA-256, and only then promote into `releases`.
Adding any transfer implementation is a separate, approval-gated change.

Example planning entry:

```yaml
offline_library_content_manifest:
  - id: example-dataset
    title: Example dataset
    kind: archive
    source_url: https://download.kiwix.org/zim/example.zim
    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    destination: sources/example.tar.zst
    enabled: false
```

## Acquisition-plan lifecycle

The acquisition workflow is deliberately staged and disabled by default:

1. **Define** a sanitized manifest with exact IDs, titles, kinds, HTTPS source URLs, SHA-256 checksums, safe relative destinations, and explicit boolean `enabled` values.
2. **Validate** the manifest. The role rejects malformed fields, non-HTTPS or non-approved Kiwix hosts, unsafe destinations, and duplicate IDs or destinations. It renders the validated manifest to `offline_library_content_manifest_path`.
3. **Approve and plan** only after the operator has reviewed the entries. Set `offline_library_acquisition_enabled: true` and `offline_library_bulk_acquisition_approved: true`. The role then reruns the storage preflight and writes `offline_library_acquisition_plan_path` from enabled entries.
4. **Acquire separately** in a future, explicitly approved workflow: download into `quarantine`, verify SHA-256, and promote verified content into `releases`. This role does not implement that transfer.
5. **Disable or roll back** by setting `offline_library_acquisition_enabled: false` and rerunning the Vancouver-only playbook. This skips acquisition planning and storage tasks without unmounting storage, deleting directories, or removing content.

`offline_library_acquisition_enabled` defaults to `false`. With it disabled, the role does not run storage preflight, create managed storage directories, or render/execute an acquisition plan. Enabling it without `offline_library_bulk_acquisition_approved: true` stops before those acquisition-related tasks. Even with both gates enabled, this role performs no network requests, content downloads, checksum promotion, or content transfer.

## Rollback and recovery

To disable acquisition without changing the storage server or mounted data, set
`offline_library_acquisition_enabled: false` in the host variables or an
approved override, then run the Vancouver-only playbook. Disabling acquisition
skips the storage preflight and managed storage-directory tasks; it does not
unmount the share, delete directories, or remove existing content.

Review the rollback first:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \
  --limit vancouver --check --diff
```

Apply only the same scoped playbook after reviewing its output. If the Ansible
implementation itself must be reverted, revert only the relevant Vancouver
offline-library commits on `vancouver/offline-library`. Do not alter the
Synology export, NFS mount, or stored content as part of this role rollback.
Storage removal or content deletion requires separate approval from the storage
owner.

Before re-enabling acquisition, verify that `/media/offline` is mounted from
exactly `NAS_HOST:/volume1/offline`, remains writable by the service account,
and meets the configured free-space threshold. The role's preflight enforces
those checks when acquisition is enabled.

## Fixture POC operations

The Vancouver fixture POC is a small, network-free smoke test. Keep the host variables explicit:

```yaml
offline_library_poc_fixture_enabled: true
offline_library_web_enabled: true
offline_library_kiwix_server_enabled: false
offline_library_acquisition_enabled: false
```

Review the Vancouver-only check-mode plan without acquisition:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \\
  --limit vancouver --check --diff \\
  -e offline_library_acquisition_enabled=false
```

Apply the same scoped playbook only after reviewing the diff:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \\
  --limit vancouver \\
  -e offline_library_acquisition_enabled=false
```

The acceptance check is the repository-owned fixture smoke test:

```shell
python3 scripts/test_poc_serving_contract.py
python3 scripts/test_poc_fixture_serving.py
```

To stop serving the POC, set `offline_library_web_enabled: false` and rerun the same
Vancouver-only command. Do not enable bulk acquisition or add bulk-content steps to
this POC procedure.

For a read-only live check on Vancouver, run:

```shell
sudo /usr/local/sbin/offline-library-poc-verify
```

It checks nginx activity, the homepage/fixture HTTP response, the deployed fixture
checksum, and consistency between the HTTP response and managed JSON report. A
nonzero exit means at least one check failed; the command does not change the host.

### Operator command sequence

Read the deployed POC status without changing Vancouver:

```shell
sudo /usr/local/sbin/offline-library-poc-status
```

Run the read-only verification command after checking status:

```shell
sudo /usr/local/sbin/offline-library-poc-verify
```

Preview the managed rollback actions without changing services or files:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \\
  --limit vancouver --check --diff \\
  -e offline_library_poc_rollback_preview_enabled=true \\
  -e offline_library_acquisition_enabled=false
```

After reviewing the preview, disable the managed POC services and configuration
only; this preserves the report and summary artifacts and never deletes content:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \\
  --limit vancouver \\
  -e offline_library_poc_rollback_enabled=true \\
  -e offline_library_acquisition_enabled=false
```

Re-enable the POC by applying the normal scoped playbook with the explicit POC
variables, then rerun the read-only status and verification commands:

```shell
ansible-playbook -i inventory/servers/hosts playbooks/offline-library.yaml \\
  --limit vancouver \\
  -e offline_library_poc_fixture_enabled=true \\
  -e offline_library_web_enabled=true \\
  -e offline_library_kiwix_server_enabled=false \\
  -e offline_library_acquisition_enabled=false
sudo /usr/local/sbin/offline-library-poc-status
sudo /usr/local/sbin/offline-library-poc-verify
```