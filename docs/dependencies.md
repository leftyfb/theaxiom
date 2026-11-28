# Application and deployment dependency inventory (#79)

Status: repository-audited baseline, 2026-11-20. Runtime validation on Vancouver
is separate from clean-install qualification. Supported development targets:
Ubuntu 24.04 LTS and 26.04 LTS, amd64 and arm64 where packages are available.
CI currently exercises Python 3.12 on ubuntu-latest, not a four-platform matrix.
Do not interpret declared minimums as tested versions on every target.

## Required server packages

These are Ubuntu package names to provision on a clean system; the release's
actual candidate versions must be recorded during qualification.

| Package | Role | Evidence |
|---|---|---|
| python3 | Backend, PDF/EPUB processing, metadata, health checks | pyproject.toml; deploy/theaxiom.service |
| python3-venv | Isolated installation | deploy/README.md |
| nginx | LAN HTTP/HTTPS frontend and authorization proxy | deploy/nginx/theaxiom.conf |
| systemd | Application service; optional ZIM acquisition monitoring | deploy/theaxiom.service; acquisition_monitor.py |
| ca-certificates | TLS trust for explicit HTTPS acquisition and preparation | pdf_transport.py; acquire_kiwix_zim.sh |
| bash, coreutils, util-linux | Shell, file/hash commands, flock, mount inspection and operational checks | scripts/acquire_kiwix_zim.sh |
| curl | HTTP health checks and external ZIM acquisition | scripts/acquire_kiwix_zim.sh |

Nginx must support http_auth_request_module and http_sub_module; do not assume
a specific package variant. Linux /proc/self/mountinfo and statvfs are used
for storage validation. Journal access for acquisition monitoring must be
restricted to an appropriate read-only service identity; do not grant blanket
sudo or change group membership automatically.

Installation/build: python3-pip, setuptools via the build manifest, and git
if preparing from a checkout. A wheelhouse or distribution archive can replace
git and internet package access on an offline server. Retain the application
checkout and templates/static assets: installing the Python package by itself
does not provide a complete standalone server distribution.

## Python dependency manifest

Canonical requirements are in [pyproject.toml](../pyproject.toml); an installer
must consume it rather than maintain a competing requirements list.

| Dependency | Current declaration | Use |
|---|---|---|
| Python | >=3.10 | Linux service runtime |
| Jinja2 | >=3.1 | HTML templates |
| PyYAML | >=6.0 | Catalog/manifests and legacy acquisition tools |
| pypdf | ==7.1.1 | PDF text extraction |
| langdetect | ==1.0.9 | Offline language classification |
| setuptools | >=68 | Build backend, not a separately required runtime tool |
| pytest, pytest-cov, ruff | dev extra | Development and CI |

Resolve transitive dependencies from package metadata (including MarkupSafe
and six). The pinned direct requirements are not a complete hash-locked
environment; collect platform-specific resolved versions and hashes before
claiming reproducibility. Check each package's Requires-Python metadata against
each target interpreter. Python 3.12 CI success is not evidence for Python
3.10 or every later Python release. Package project sources: PyPI and the
respective upstream projects. Preserve distribution licenses and notices.
PDF language inference runs with a bounded subprocess; missing parser/model
packages degrade inference rather than preventing content acquisition.
See [PDF language detection](pdf-language.md) for limits and operator preparation.

## Adapter-specific components

| Feature | Requirement | Scope |
|---|---|---|
| Offline HTTPS | openssl; appliance-local CA and server certificate; Nginx TLS support | Optional TLS provisioning/renewal; recommended for administrator access. See [deployment steps](../deploy/README.md#offline-https-local-certificate-authority). |
| ZIM/Wikipedia | kiwix-serve (candidate Ubuntu package kiwix-tools), compatible libkiwix | Separate local service on 127.0.0.1:8081 |
| ZIM acquisition | curl, bash, coreutils, util-linux; systemd and journal monitoring | Optional, explicitly approved external acquisition only |
| PDF reading | Mozilla PDF.js legacy generic distribution, pinned 6.5.438 | Local static asset distribution at /opt/pdfjs/current |
| PDF language | pypdf and langdetect as above | Bundled offline model, no OCR |
| EPUB reading | Python standard library and application templates | No external EPUB reader dependency |
| NFS storage | nfs-common | Only for NFS client deployments |
| SMB storage | cifs-utils, candidate only | Not required for current NFS deployment; validate separately |

The Nginx viewer customization targets libkiwix 14.0.0. This is a compatibility
reference in the config, not a verified installed kiwix-tools package version.
Record the kiwix-serve executable location, version and package owner on the
actual host; if installed outside dpkg record the upstream archive and hash.
Do not substitute an arbitrary newer viewer for the tested shell without checking
navigation and authorization. Kiwix upstream: https://kiwix.org/ .

PDF.js installation source, release URL, expected SHA-256 and license are
canonical in [pdfjs.md](pdfjs.md). Use that pinned distribution, not an
unverified Ubuntu libjs-pdf or pdf.js-common candidate. Those package names,
versions and availability are not established as suitable replacements.
Install only on a connected preparation host or from verified offline media;
no CDN or runtime fetch is necessary. Keep versioned assets root-owned and
not writable by the service account.

## Development and deployment controller

- Python dev extra and Node.js (JavaScript regression harness); CI actions
  checkout/setup-python v4/v5 and a GitHub-hosted runner with gh, bash and
  standard text utilities. Node.js is not required to serve the application.
- Controller: git, openssh-client, bash, coreutils, util-linux and gh for the
  existing issue/poller workflow. SSH server is needed on a target only for
  that deployment method. Transfer via Git bundle is the current observed
  deployment flow; rsync is not established as required. No automation,
  Hermes, or Projectwright is an application runtime requirement.
- wget/unzip are optional preparation tools for upstream PDF.js artifacts;
  they are not required by the reader itself. Build tools may be necessary
  if a dependency lacks wheels for the target architecture.

## Clean-install preparation

The following is a proposed package preparation list, not a command to run
on an existing deployment. On a fresh authorized host first inspect candidate
versions and confirm all packages for its OS/architecture. Optional packages
must be selected according to features.

```sh
apt-cache policy python3 python3-venv python3-pip nginx systemd ca-certificates \
  bash coreutils util-linux curl git kiwix-tools nfs-common nodejs openssl
# On a clean authorized host only, after package review:
# sudo apt install python3 python3-venv python3-pip nginx systemd \
#   ca-certificates bash coreutils util-linux curl
# Optional offline HTTPS provisioning: sudo apt install openssl
```

Prepare a wheelhouse for the target Python/architecture using the canonical
manifest and resolved pinned dependencies. Include build prerequisites and
the release checkout, verify hashes, preserve notices and record OS package
versions (and provide the matching debs or a signed local repository for an
offline installation). Test a fresh virtual environment against that local
wheelhouse using pip --no-index --find-links and --no-build-isolation after
installing required build tools locally. Run pip check, then the preflight
below. The required account, paths, permissions and Nginx setup are documented
in [standard deployment](../deploy/README.md). Do not create or modify a mount,
change ownership recursively, acquire content, or replace active configuration
during a dependency audit.

## Read-only host verification

Run locally on Vancouver with an authorized operator account. These commands
report versions and paths, not passwords, configuration secrets or content.
Do not run apt update/install or initiate archive downloads.

```sh
. /etc/os-release
printf 'OS=%s VERSION=%s\n' "$ID" "$VERSION_ID"
dpkg --print-architecture
for p in python3 python3-venv python3-pip nginx systemd ca-certificates \
         bash coreutils util-linux curl git nfs-common kiwix-tools openssl; do
  dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\n' "$p" 2>/dev/null || true
done
nginx -V 2>&1
command -v kiwix-serve && kiwix-serve --version
readlink -f /opt/pdfjs/current
sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python --version
sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -m pip check
sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -c \
  'from importlib.metadata import version; print({p: version(p) for p in ("Jinja2","PyYAML","pypdf","langdetect","six","MarkupSafe")})'
sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -c \
  'import theaxiom.admin_api, pypdf, langdetect; print("imports OK")'
systemctl is-active theaxiom.service nginx
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

For Kiwix identify the package that owns the resolved executable with
dpkg-query -S, or record the external binary provenance. Check the PDF.js
versioned directory and required renderer/worker assets read-only against
the installation instructions; the local directory name is not an integrity
proof. Compare nginx -V features with the configured directives. Record
missing optional components separately from required failures. Do not
publish raw environment or configuration files.

## Offline verification and outstanding qualification

CI fixture tests exercise local serving, publication boundaries, readers,
PDF extraction and metadata without external content downloads. Accepted
Vancouver deployments verify health, service imports, reader responses,
storage mount and authorization (AC-127); owner accepted PDF text search,
language detection and admin uploads. This does not establish that WAN
was disconnected during those tests. A genuine isolated-network smoke test
on a disposable clean installation must verify local homepage, admin,
PDF.js assets and representative PDF/EPUB/ZIM reading while checking that
browser requests stay on the LAN. Do not disconnect an existing host or
change firewall rules for this audit.

Still needed before full #79 acceptance: actual Vancouver package/version
output, package availability and clean-install tests on Ubuntu 24.04/26.04
amd64/arm64, resolved wheel/deb hashes, external Kiwix provenance, and isolated
network smoke evidence. Track offline installation/recovery in #53 and
standard deployment in #10. No new installer or host mutation is authorized.


### Operator-selected shared-directory permissions

The Axiom accepts application-owned content leaf directories with mode `0777`,
as well as restrictive modes. Configured mount identity, directory ownership,
path/symlink restrictions, free-space reserve and individual installed-file
ownership/permissions remain enforced. World-writable directories allow other
local users to rename or remove directory entries, including installed content;
this can cause disruption even when the file checks reject substituted bytes.
Use restrictive permissions on untrusted multi-user hosts, but the application
does not silently change an operator's chosen directory mode.

For scoped educational ZIM generation, install the Ubuntu `zim-tools` package
(`zimwriterfs`) alongside the existing `kiwix-tools` package. Verify its
availability and version before enabling the generated archive adapter.
