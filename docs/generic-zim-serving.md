# Generic ZIM publication: acceptance and deployment gate

**Deployment hold:** Do not deploy, restart services, touch Vancouver, or create a deployment request until the operator explicitly confirms the active Ready.gov download has completed and authorizes deployment.

## Design

The existing Wikipedia Kiwix reader stays on loopback port 8081. An independent optional generic Kiwix reader would bind only to 127.0.0.1:8082 behind the separate /zim/ Nginx publication gate. Its systemd file (deploy/theaxiom-kiwix.service) is committed as an example, not installed or activated.

Generic ZIM downloads are source-scoped, SHA-256 checked, and promoted after ZIM internal checksum verification. Download completion never implicitly publishes an archive. The operator must explicitly publish and confirm redistribution rights. The private kiwix-library.xml includes only currently published, verified installed archives, and is refreshed after publication changes. Kiwix monitors that file; the Axiom authorizes serving only when a separate OPDS probe verifies the exact filename-derived content ID set and writes a matching runtime generation acknowledgement.

The browser-facing route /read/zim/<source-id> resolves the installed filename-derived book name, not the human catalog source ID. Every request for /zim/content/<installed-filename-stem>/... is guarded against the current verified and published set. Missing, stale or unacknowledged manifests fail closed. Generic publication does not change the existing Wikipedia reader.

## Isolated CI

Application CI generates a small real ZIM with zimwriterfs and launches a real loopback kiwix-serve, checking OPDS and viewer pages. The service is also tested while library entries are added and removed. Independent Nginx tests validate the actual site grammar and exercise a private publication subrequest that blocks an unpublished content request before it reaches the Kiwix upstream. All of this runs on isolated GitHub Actions runners, not Vancouver.

The ZIM acquisition suite also covers restart/resume using persisted range validators, rejected changed validators, partial retention, cancellation cleanup and completed-archive protection. Existing 0777 managed storage directory modes are an accepted operator policy; code may validate owner, mount and individual-file integrity but must never chmod them to 0700.

## Live acceptance checklist (ONLY after the deployment hold is lifted)

1. Confirm Ready.gov completed and the full archive was verified; record deployed SHA and preserve all partial and installed files. Do not restart an active acquisition.
2. Confirm the installed kiwix-serve/libkiwix version reads the generated XML manifest. Note that libkiwix 15 plans to drop legacy library XML in favor of OPDS; a migration must be separately developed and verified before upgrading to an incompatible binary.
3. Review and explicitly approve the optional 8082 service and new Nginx configuration, validate nginx -t, and verify Wikipedia on 8081 still retains its original search, home navigation and deep links.
4. Use a separate small test ZIM to check acquisition, verified state, Publish/Unpublish, rights acknowledgement, reader redirects, mobile viewing, direct content links, asset delivery and search.
5. With two published ZIMs, test automatic Kiwix library reload and ensure an unpublished or removed book becomes inaccessible, including after reader or backend restarts.
6. Test pause, resume and cancel with a NEW small transfer, not the current Ready.gov download. Check that resume starts at the retained byte offset, verifies the entire file and that cancel never touches installed archives.
7. Check user-configured directory modes remain unchanged, including 0777, and that deployment rollback preserves state and any staged bytes.

#2 and #86 must remain open for final live acceptance. Passing isolated CI is not a production deployment.
