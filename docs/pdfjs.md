# PDF.js integration (#74)

The official Mozilla legacy generic distribution is pinned to 6.5.438.
The preparation script verifies the release archive's exact size and SHA-256
before extraction, refuses unsafe paths and symlinks, and requires the viewer
and worker entry points and license. The read-only preparation workflow runs
on changes to the script/workflow and produces a reviewable distribution artifact.

This is dependency preparation only. The application does not yet consume
these assets; no runtime internet requests or new deployment services are
introduced. Before enabling the viewer, vendor the verified distribution
and upstream notices into the application checkout, inspect its license and
browser requirements, enforce publication and external-link/scripting
restrictions, and test the actual reader on desktop and mobile.
