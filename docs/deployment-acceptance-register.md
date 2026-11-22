# Development-to-deployment acceptance register

This is an operator acceptance checklist, **not deployment authorization**. Review the current `main` SHA, exact-commit CI, coordination acceptance, and live revision before any deployment. **CI success is not evidence of a successful deployment.**

## Current baseline (2026-11-22)

- **Last independently confirmed Vancouver application revision:** `8d1f9b506698275c4849abd6f5a186f19d2cc561`. Licensing deployment AC-166 was completed and closed in [coordination #706](https://github.com/leftyfb/agent-coordination/issues/706).
- **Latest reviewed development revision:** `721cec8bfdc0fb68a0cb2b20bbbd67c6d60a9a2b`. [Application CI run 45061109565](https://github.com/leftyfb/theaxiom/actions/runs/45061109565) passed for this revision. Its two README-only commits (`80e12c3` and `721cec8`) have **not** been confirmed deployed.
- **Current deployment authorization:** None for revisions following AC-166. Request an explicit new authorization for the exact application target, after current CI and scope review. Do not replay completed deployment requests.
- **Current work awaiting implementation/acceptance:** [#7](https://github.com/leftyfb/theaxiom/issues/7), authoritative remote-update detection. Do not infer that unfinished functionality is deployed or safe to exercise on production archives.

These values are a dated snapshot, **not** an evergreen assertion of live state. Confirm current facts through GitHub Actions, coordination, and the deployed health/revision endpoint at each deployment.

## Operational safeguards for every deployment

1. **Authorization and provenance.** Record the exact target commit, exact successful CI run, changes from deployed baseline, and an explicit operator approval. Use a new uniquely identified coordination request. Never treat a green test run or an older authorization as blanket permission.
2. **Preflight before mutation.** Verify the current live revision, health, publication state, storage mount/source, Nginx and systemd state, running acquisitions, current Kiwix catalog and installed content. Abort on mismatch, missing evidence, or unsupported diff scope.
3. **Protect content and configuration.** Preserve the installed ZIM/PDF/EPUB bytes, download staging/partials, catalog, session and publication data, rights acknowledgements, and managed NFS storage. Never silently retry, truncate, rehash, relocate or discard a large archive. Catalog removal does not delete installed bytes. Do not chmod/chown existing managed directories, including operator-selected 0777 modes.
4. **Restrict configuration changes.** For any changed Nginx or systemd configuration, require the script's applicable exact-content approval and safety checks; do not wildcard/bypass allowlists. Preserve backup, `nginx -t`, restart/health checks and rollback paths. An unchanged Nginx file does not require inventing a new Nginx migration.
5. **Verify live behavior after deployment.** Check deployed exact SHA, health, Wikipedia and other published Kiwix routes (including deep links and search), PDF/EPUB readers as relevant, admin authentication, and publication/unpublication gates. Ensure existing installed content remains available and no acquisition was interrupted or restarted. Record evidence and rollback outcome as needed.
6. **Separate CI from live acceptance.** Automated browser, Kiwix, and Nginx regression suites run in CI and reduce repeated manual testing, but do not prove the target server's storage, mounts, services or deployed revision.

## Historical incidents (closed; not current holds)

- On 2026-11-20, [AC-135 #285](https://github.com/leftyfb/agent-coordination/issues/285) and [AC-137 #291](https://github.com/leftyfb/agent-coordination/issues/291) were blocked before deployment because the local file-scope and exact Nginx approval preflights did not accept their proposed changes. These **closed historical requests must not be replayed**. The fixed script's allowlists and exact configuration checks still apply.
- The earlier Ready.gov invalid-header investigation (#87) was deferred to preserve data, and later closed after resolution. Historical preservation instructions are not evidence of a currently failed or active transfer. Do not touch archived bytes without explicit owner instruction.
- Generic ZIM reader and pause/resume/cancel work ([#2](https://github.com/leftyfb/theaxiom/issues/2), [#86](https://github.com/leftyfb/theaxiom/issues/86)) are closed. The shared reader is documented in [generic ZIM serving](generic-zim-serving.md); re-test relevant regressions when changing reader or acquisition code, rather than re-imposing obsolete migration gates.

This register deliberately avoids prescribing a deployment command or authorizing a deployment.
