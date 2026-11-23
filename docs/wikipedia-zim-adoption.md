# Adopt Vancouver's existing Wikipedia ZIM in place

The pinned archive remains at its existing path. Adoption writes a private
journal and a completed job in the managed ZIM ledger. It does not change
the archive's directory entry, permissions, or content. The archive's exact
identity is checked against the ledger when opening the installed file.

## Before the cutover

Deploy the exact CI-verified application revision through the approved
deployment path. Keep the legacy service and old reader working through that
deployment. Verify the expected live revision, existing Wikipedia and FEMA
reader and search, and that the legacy acquisition unit is inactive and
successful. Hold the existing deployment lock for the cutover through
acceptance or rollback. Pause competing publication changes and prevent the
legacy acquisition from starting using the approved host procedure. Record
its original enablement state and preserve the deployed version, publication
configuration, and Kiwix manifest for rollback.

The cutover runs under the existing Axiom service account and environment.
Verify that this account can read the current invocation's acquisition
journal before proceeding. If it cannot, stop and report the exact error.
The command requires the pinned archive on the configured mount, trusted
current-invocation evidence, and a stable regular file with one link. It
refuses conflicting jobs and replacement candidates.

## Host commands for the cutover

Run these on Vancouver under the existing host authorization. Run the
deployment lock and legacy-unit checks above before entering this sequence.
The private journal is written beside the existing managed ZIM jobs.

```bash
sudo systemd-run --wait --pipe --collect \
  --property=User=theaxiom --property=Group=theaxiom \
  --property=WorkingDirectory=/opt/theaxiom/app \
  --property=EnvironmentFile=/etc/theaxiom/server.env \
  /opt/theaxiom/app/.venv/bin/python -m theaxiom.zim_adoption adopt \
  /media/offline/library/zim/wikipedia_en_all_maxi_2026-08.zim
```

A successful command prints `completed`. If interrupted after writing a
prepared journal, the same command can finish against the exact original
file and recorded job. It refuses divergent state.

Reconcile the shared Kiwix manifest with the existing application helper:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=User=theaxiom --property=Group=theaxiom \
  --property=WorkingDirectory=/opt/theaxiom/app \
  --property=EnvironmentFile=/etc/theaxiom/server.env \
  /opt/theaxiom/app/.venv/bin/python -c \
  'from theaxiom import admin_api, zim_refresh; raise SystemExit(not zim_refresh.reconcile(lambda: admin_api.SOURCE_CATALOG.get()["sources"]))'
```

Check that Wikipedia appears once in OPDS, the managed Admin controls and
metadata-only update check work, and historical article and viewer URLs
still resolve. The original filename and Kiwix book ID stay valid during
adoption. Old book-ID links are tied to that edition and may stop resolving
if a later replacement changes the Kiwix book ID. Check cross-source search and FEMA. Confirm the original
inode, file size, and publication intent. Keep the legacy unit from starting
after acceptance under the authorized host procedure.

## Rollback

If acceptance fails, keep the locks and run the same service-account
invocation with `-m theaxiom.zim_adoption rollback`. Then run the same
reconciliation command and check legacy Wikipedia and FEMA. Restore the
recorded legacy unit enablement without starting it. The rollback removes
only the exact adoption job. A divergent job or replacement requires review.
