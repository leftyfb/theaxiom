# Wikipedia ZIM adoption runbook

This runbook is the operator procedure for The Axiom #9. It converts one
already verified legacy Wikipedia ZIM into the ordinary managed ZIM ledger. It
is a **cutover**, not an acquisition: the existing inode is moved within the
same directory with a no-overwrite hard-link/unlink operation. No archive bytes
are downloaded, copied, read end-to-end, or rehashed.

Run only from an explicitly authorized Hermes session on `vancouver`, using the
host-side scope approved for this ticket. This document does not grant access
or deployment authorization. If the scoped executor, locks, backup location,
or tested manifest procedure is unavailable, stop before the first mutation.

## Fixed scope and acceptance target

Only this source may be changed:

| Item | Required value |
| --- | --- |
| Source ID | `wikipedia-en` |
| Legacy path | `/media/offline/library/zim/wikipedia_en_all_maxi_2026-08.zim` |
| Managed filename | `wikipedia-en.34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6.zim` |
| Expected length | `127418087648` bytes |
| Pinned SHA-256 provenance | `34162d18b9f96f494eac48a73a309c72e0bc95d85f76be405d0f5de795854aa6` |
| Source URL | `https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim` |
| Legacy unit | `acquire-wikipedia-zim.service` |
| Public reader | shared Kiwix on port `8081`, behind the normal publication gate |

Preserve FEMA and every other source, the configured NFS mount and source
profile, publication and rights state, public bookmarks, old book IDs, and deep
article URLs. Do not alter permissions, mounts, unrelated files, or the
acquisition plan.

The pinned digest is **provenance**, not permission to hash the live archive.
The cutover must establish identity from the anchored file handle and the
already recorded successful acquisition evidence. A bounded header read to
retain the Kiwix UUID is permitted; a full-archive read is not.

## Command and evidence conventions

The application command is installed from the exact deployed application
revision:

```sh
APP=/opt/theaxiom/app
PY=$APP/.venv/bin/python
LEGACY=/media/offline/library/zim/wikipedia_en_all_maxi_2026-08.zim
JOURNAL=/var/lib/theaxiom/cutovers/wikipedia-en-adoption.json
SOURCE_URL=https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim
```

The target CLI derives success evidence from the scoped systemd unit and its
current invocation. It does **not** accept an external marker-path argument.
Use the deployed command shape:

```sh
sudo -u theaxiom "$PY" -m theaxiom.zim_adoption adopt \
  "$LEGACY" "$JOURNAL" "$SOURCE_URL"
```

For rollback:

```sh
sudo -u theaxiom "$PY" -m theaxiom.zim_adoption rollback "$JOURNAL"
```

The repository revision being changed may still show the compatibility API
that accepts `legacy_path marker_path journal_path source_url`; do not use that
old marker-file form for production. The accepted production API is the
systemd-derived form above. The command must fail closed if the unit, invocation
or journal evidence is absent, stale, ambiguous, or does not certify the pinned
path, length, and digest.

Capture command output and exit status in the private cutover journal, but do
not put credentials, complete journal payloads containing sensitive paths, or
large command output in the ticket.

## Repository validation (before any host work)

Use the existing Application CI workflow (`.github/workflows/ci.yml`) on a PR
for Python tests and the isolated Kiwix, Nginx and Chromium checks. Keep
synthetic test archives in runner temporary directories. Do not install or run
the Axiom test runtime on tux or use Vancouver to try out the migration.
Before deploying, record the successful CI run for the exact deployment commit
on `main` in #948. Report any live-only blocker separately from CI status.

The existing archive parent on Vancouver was observed as `0777` with an ACL
allowing other principals to write. That observation does not imply that normal
managed serving or promotion is blocked. For this cutover, stop before writes
if the existing permissions and effective ACLs allow uncoordinated writers to
replace a directory entry between identity validation and unlink. Advisory
locks only coordinate cooperating processes. Do not change the archive parent
mode or ACL to proceed.

## Checkpoint C0 — authorization and exact revision

Before touching the host:

1. Confirm the approved target application commit, exact successful CI run, and
   normal exact-content deployment approval. A green CI run alone is not live
   authorization.
2. In `/home/leftyfb/repos/theaxiom`, verify a clean checkout, pinned origin,
   and the exact commit to deploy. Do not deploy an unexpected branch or dirty
   tree.
3. Record the live revision before deployment:

   ```sh
   curl --fail --silent --show-error http://127.0.0.1:8080/health
   systemctl show theaxiom.service --property=ActiveState,SubState,ExecMainStatus
   ```

   The health JSON must report the expected SHA. The known prior baseline was
   `e87724090e56e310ec6ef00469d4d8c6e63a4b1a`; treat any current mismatch as a
   stop condition, not as permission to proceed.
4. Deploy application/configuration code only through the established pinned
   procedure and its normal Nginx/systemd approval gates. Keep the legacy reader
   path working at this checkpoint. Ordinary app deployment must perform no
   migration writes.
5. Recheck `/health` after deployment and record the exact live SHA. If the
   deployed SHA is not the authorized SHA, stop before cutover.

**Why an app commit alone does not cut over:** the application code does not
own the legacy archive, its systemd acquisition unit, the NFS mount, the Kiwix
manifest, or the publication gate. Until the host-side evidence is checked and
the inode is adopted under locks, the normal managed ledger still has no
Wikipedia artifact and Kiwix still has its prior manifest. A deployed commit
can therefore be healthy while the legacy archive remains outside the managed
controls. The cutover is a separate, authorized host mutation.

## Checkpoint C1 — acquire locks and freeze competing work

Use `/run/lock/theaxiom-deploy.lock` (the deployment wrapper's lock) and the
legacy acquisition helper's `${LEGACY}.lock`. Verify the lock files and their
owners before taking exclusive nonblocking locks on the existing inodes.
Record their paths and owners in the private journal. If either lock cannot be
verified or acquired, stop. Keep both locks until acceptance or rollback is
complete. Do not invoke the deployment wrapper while holding its lock.

Under the locks:

```sh
systemctl show acquire-wikipedia-zim.service \
  --property=LoadState,ActiveState,SubState,Result,ExecMainStatus,InvocationID,ControlGroup
```

Require no active acquisition and no competing cutover. The legacy unit must be
`inactive`, have `Result=success`, and have `ExecMainStatus=0`. Record whether
the unit was enabled before the cutover (`systemctl is-enabled ...`) and whether
it was masked/disabled by the approved procedure. Do not start or restart it.

The acquisition helper's per-destination lock is also relevant: the legacy
path's `.lock` must not be held by a running acquisition. Do not remove a lock
file; inspect ownership/state through the approved host procedure.

## Checkpoint C2 — mount, file identity, and bounded systemd evidence

Verify the configured storage settings and actual mount before examining the
archive:

```sh
findmnt -T /media/offline -o TARGET,SOURCE,FSTYPE,OPTIONS
stat -c '%F %a %u:%g %h %s %d:%i %Y %Z %n' -- "$LEGACY"
```

Require the configured target `/media/offline`, the configured source, an
`nfs`/`nfs4` filesystem, and a regular one-link file owned by the expected
service identity with safe mode bits and exactly `127418087648` bytes. The
adoption implementation must repeat these checks through an anchored
`O_NOFOLLOW` file descriptor immediately before moving the inode and again on
the managed name afterward.

Read only the current invocation's bounded journal evidence:

```sh
INVOCATION=$(systemctl show -p InvocationID --value acquire-wikipedia-zim.service)
systemctl show acquire-wikipedia-zim.service \
  --property=ActiveState,SubState,Result,ExecMainStatus,InvocationID
journalctl --no-pager -o json -n 30 \
  -u acquire-wikipedia-zim.service \
  "_SYSTEMD_INVOCATION_ID=$INVOCATION"
```

Re-read `systemctl show` after the journal query and require the complete unit
state, including `InvocationID`, to be unchanged. Evidence must be a bounded
success record for the exact legacy filename, pinned length and pinned digest,
with a journal timestamp newer than both the archive mtime and ctime. If the
invocation is missing, changed, rotated, stale, or ambiguous, stop. Do not
substitute file size, a filename, an old external marker, or a new full hash.

A target-side CLI may derive and retain this evidence itself; operators must
not manufacture a marker file. Preserve the returned invocation ID and evidence
summary in the adoption journal.

## Checkpoint C3 — backup and rollback readiness

Create a private, root-controlled, mode-0700 backup directory named with the
cutover ID. Back up only small mutable state, never the ZIM:

- exact application revision and `/health` response;
- source/catalog entry for `wikipedia-en`;
- ordinary ZIM job record, or a recorded cryptographic statement that it is
  absent;
- Kiwix manifest and publication/rights state;
- relevant Nginx/systemd configuration if it is in scope;
- old unit enabled/masked/disabled state;
- mount/source and anchored archive identity (`device`, `inode`, bytes, mtime,
  ctime);
- the bounded systemd evidence and the planned rollback steps.

Use the host's approved metadata-backup command/procedure. Do not use `cp` or
`rsync` on the 127 GB archive, do not create a second archive copy, and do not
rehash it. Verify each backup is readable by the authorized operator and record
its path before the first mutation. Record that the original filename is
expected to become vacant during adoption; rollback is allowed to restore it
only when the inode checks pass and the path is vacant.

Stop if the managed destination already exists, any ordinary `wikipedia-en` job
is present with conflicting state, the catalog or publication state changed, or
any backup is incomplete.

## Checkpoint C4 — locked adoption

With both locks held and the legacy unit prevented from starting by the
approved host procedure:

```sh
sudo -u theaxiom "$PY" -m theaxiom.zim_adoption adopt \
  "$LEGACY" "$JOURNAL" "$SOURCE_URL"
```

The command must:

1. revalidate the exact mounted parent and anchored inode;
2. derive current-invocation systemd evidence, without an external marker path;
3. write a durable `prepared` journal before movement;
4. create the managed name without overwrite, using the same-directory inode
   move; and
5. durably save the ordinary completed, verified `wikipedia-en` job with exact
   artifact identity, source URL, pinned provenance, and `adopted_from_legacy`.

It must never download, copy, overwrite, chmod/chown, mount, delete the legacy
bytes, or calculate a full archive digest. A crash may leave `prepared` or
`moved` state; rerun the same command under the same locks for its exact,
identity-checked recovery. Do not manually repair either pathname.

After success, verify that the legacy path is absent, the managed path exists,
and both names were never simultaneously accepted as separate archives. The
managed file must have the original device, inode, length, and mtime. The job
must be visible through the ordinary installed-ledger and admin control paths;
a raw job entry or compatibility registry entry is insufficient.

## Checkpoint C5 — manifest and publication refresh

Leave publication intent, rights acknowledgement, source profile, and public
bookmarks unchanged. Refresh the shared Kiwix manifest using the existing,
tested host procedure; do not invent a new command or alter unrelated entries.
Wait for the exact OPDS acknowledgement. Restart Kiwix only if the tested
procedure requires it and the host authorization explicitly covers that
restart. Do not restart the acquisition unit or the application merely to make
the archive appear.

Record the manifest backup, refreshed manifest identity, OPDS acknowledgement,
and any Kiwix unit status in the private journal. If manifest refresh fails,
enter rollback; do not retry with a different archive name or copy.

## Checkpoint C6 — acceptance and the 60-second rollback window

Start the postflight timer immediately after manifest refresh/OPDS
acknowledgement. All checks below must pass within **60 seconds**:

- `/health` reports the authorized exact live SHA and `theaxiom.service` is
  healthy;
- Admin ZIM controls show one installed, verified `wikipedia-en` record through
  the ordinary managed path;
- the original publication and rights state is unchanged;
- OPDS contains one Wikipedia book, not a duplicate registration;
- the old viewer/book ID, old public entry, browser-fragment deep article URL,
  native search link, and normal publication gate work;
- unpublished access remains denied where it was denied before;
- public global search returns both a Wikipedia result and an existing FEMA
  result;
- one metadata-only managed update check reports the installed artifact and
  does not download or modify it;
- the managed file has the expected length and the same device/inode as the
  adoption result; and
- `acquire-wikipedia-zim.service` remains inactive and no acquisition process
  ran or restarted.

Use bounded status/HTTP/OPDS requests and the approved browser acceptance
procedure. Do not run `sha256sum` on the archive. Save only concise evidence:
status, timestamps, route/result, unit state, invocation ID, and inode metadata.

If any acceptance check fails, is ambiguous, or exceeds 60 seconds from
manifest refresh, do not attempt another migration or a fresh verification.
Proceed immediately to rollback under the same locks.

If all checks pass, record the acceptance timestamp and leave the legacy
acquisition unit disabled only if that action is covered by the host
authorization. Keep the old files, evidence, and backups available. Removing
compatibility code or unit files requires a separate exact-CI app/config change
and a separately accepted deployment.

## Checkpoint C7 — rollback

Rollback is required for any failed postflight, manifest failure, unexpected
reader/publication behavior, or operator-detected divergence. Keep both locks.
Use the journal created by adoption:

```sh
sudo -u theaxiom "$PY" -m theaxiom.zim_adoption rollback "$JOURNAL"
```

The rollback command is fail-closed. It must verify the managed and/or legacy
path refers to the exact adopted inode, refuse changed/replaced inodes, record
`rolling_back` before movement, restore the original filename only if it is
vacant, and remove only the exact adopted ordinary job record. It must not
overwrite a path, delete an unrelated job, redownload, rehash, or start an
acquisition.

Restore the backed-up manifest, publication/rights state, and app/configuration
revision only when the failure requires it and the approved host procedure
covers that restoration. Restore the old acquisition unit's original
enablement state without starting it. Re-run the old reader, Wikipedia and FEMA
routes, search, publication gate, and `/health` checks. Confirm the original
filename is present with the recorded inode identity and the managed name is
absent, or stop with both paths preserved if the state is not safe.

A crash can leave a prepared/moved/rolling-back journal. Retry the same
idempotent command under the locks; never manually unlink, rename, overwrite,
or reconstruct the archive. If inode or path checks prevent rollback, stop and
report the exact state, both pathnames, journal state, and backup location.

## Closeout record

Post the ticket only after acceptance or a documented rollback attempt. Include:

- exact application commit and CI run;
- live health SHA before and after;
- lock paths and preflight results;
- mount/source, unit enablement, invocation-scoped evidence summary, and inode
  metadata;
- private backup location (not sensitive payloads);
- adoption journal state and managed filename;
- bounded 60-second postflight results and OPDS acknowledgement;
- whether the legacy unit was left disabled; and
- rollback outcome, or an explicit partial/failure state with both paths
  preserved.

Keep The Axiom #9 open until the migration and all acceptance evidence are
reviewed and accepted.
