# Application CI performance (issue #133)

## Measuring a run

From the checkout root on the same runner, use the same interpreter and
installed dependencies, idle runner conditions, and a fresh test temp path
for each revision. Do not benchmark while another CI job is running.

```sh
mkdir -p .cache/ci-profile/tmp
export TMPDIR="$PWD/.cache/ci-profile/tmp"
# Give each revision a different label; pytest replaces an existing basetemp.
label=before
/usr/bin/time -f 'pytest wall=%e exit=%x' .venv/bin/python -m pytest -q \
  --basetemp=".cache/ci-profile/pytest-$label" --durations=0 --durations-min=0 \
  --junitxml=".cache/ci-profile/pytest-$label.xml" \
  > ".cache/ci-profile/pytest-$label.log" 2>&1
```

Use the duration log for per-test setup/call/teardown attribution and the
JUnit `testcase` times for per-module totals and comparing test IDs. The
JUnit suite total includes subtests; pytest's footer shows tests and subtests
separately. CI prints only the 15 slowest phases and includes JUnit alongside
its existing pytest log. For development feedback, first run the relevant
`tests/test_*.py` files; this is not a substitute for exact-head full CI.

Time each of the existing fixture scripts separately with `/usr/bin/time`:
`tests/ci_test_kiwix_live.py`, `tests/ci_test_nginx_syntax.py`,
`tests/ci_test_nginx_auth.py`, `tests/ci_test_browser.py`, and
`tests/ci_test_readers_browser.py` (which also runs the map reader smoke).
Set `AXIOM_PROFILE_BROWSER=1` for per-journey `BROWSER_TIMING` lines. Set
`AXIOM_BROWSER_ARTIFACTS` to a local scratch path and use the pinned PDF.js
fixture as described in [Browser regression testing](browser-regression.md).
These checks use synthetic content, local HTTP and disposable publication state.

## Tux measurement, 2026-11-27 UTC

Baseline `f624b0bae096cecda4ebf9512163ec7112470324`, Python 3.14.9,
pytest 9.2.0, serial runs, one sample per full pytest revision and the same
`--durations=30` plus JUnit on both. Wall time fell from 224.49 s to
153.75 s (70.74 s / 31.5%). Both had **952 tests and 196 subtests pass**, with
identical JUnit testcase IDs across 60 modules and zero skipped/failed.

| Category / module | Baseline seconds | Candidate seconds |
| --- | ---: | ---: |
| HTTP acquisition, update/rollback and publication (`test_source_profiles_http`) | 67.71 | 38.93 |
| Authentication (`test_admin_security`) | 36.75 | 30.31 |
| Browser session security (`test_admin_sessions`) | 30.26 | 3.05 |
| PDF source lifecycle (`test_pdf_sources`) | 17.11 | 12.71 |
| ZIM jobs (`test_zim_jobs`) | 17.08 | 15.37 |
| Admin page template (`test_admin_page_template`) | 11.09 | 10.91 |
| PDF language/search input (`test_pdf_language`) | 10.89 | 9.88 |
| Legacy entrypoint contracts (`test_legacy_script_entrypoints`) | 10.42 | 10.42 |
| EPUB sources (`test_epub_sources`) | 3.25 | 2.37 |

Module rows are sums of JUnit testcase durations; single-sample differences
in untouched modules are not attributed to these edits. All top 30 phases
reported on both runs were calls. The source-profile fixtures close inside
calls, as does `unittest`'s `tearDown`; this is why shorter HTTP shutdowns
show up as call-time savings. In a loopback microbenchmark (12 fresh servers
per arm, one GET, shutdown and thread join), median startup through first GET
was 2.7 ms → 2.3 ms; median shutdown was 500.0 ms → 49.8 ms with a 50 ms
fixture-only poll interval. Per-test server and state isolation are unchanged.
The bounded-session test's call fell from 28.56 s to 1.30 s: it checks the exact
credential bytes after every issuance and still verifies the real password
before and after the loop. This retains coverage while removing repeated
PBKDF2 checks against an identical record.

No tests were removed or skipped. The legacy entrypoint tests still cover
supported invocation contracts. The browser cases retain desktop, narrow and
mobile coverage (plus reader tablet), independent auth sessions, native search,
PDF/EPUB and map rendering, print, and publication denial. The reader search
retry polls real asynchronous indexing; it is not an unconditional wait.
Separate browser contexts, cold fixture state and viewport-specific assertions
are intentional. No browser repetition or waits were removed. Production server
settings, deployment gating, and trusted runner selection are unchanged.

With the candidate and preinstalled dependencies on tux (Playwright 1.65.2),
the five real fixture checks passed: Kiwix 6.86 s, nginx syntax 0.12 s, nginx
publication gate 0.77 s, Chromium usability 101.03 s, and Chromium readers
including MapLibre 76.09 s. The two longest usability journeys were desktop
navigation (26.83 s) and desktop source actions (23.15 s); history polling
took 4.80–4.91 s per viewport. PDF/EPUB took 15.85 s desktop, 14.31 s tablet,
14.31 s mobile and 14.47 s narrow; reader bulk actions took 8.90 s. The sum
of pytest and the five checks was 338.62 s, excluding installs and reporting.
The exact-baseline CI below gives context for browser timings, not a matched
browser speedup measurement. Browser edits add timings only.

## Job time versus queue time

Use bounded `gh api repos/leftyfb/theaxiom/actions/runs/RUN/attempts/ATTEMPT/jobs`
reads. For each job subtract `created_at` from `started_at` for queue time, and
`started_at` from `completed_at` for execution. Treat rerun gaps separately.
These are elapsed queue times, not proof of why GitHub queued an individual job.

| Run / attempt | Test queue | Test job | Report queue | Report job |
| --- | ---: | ---: | ---: | ---: |
| `45936719385/1` (exact baseline) | 0 s | 433 s | 1 s | 19 s |
| `45914480572/1` | 1 s | 403 s | 1 s | 19 s |
| `45910064353/1` | 1 s | 394 s | 1 s | 18 s |
| `45864756328/1` (canceled in pytest) | 1 s | 149 s | 1 s | 19 s |
| `45714857681/1` (reader print assertion failed) | 1 s | 343 s | 1 s | 17 s |
| `45714857681/2` (passed) | 2 s | 352 s | 1 s | 18 s |
| `45207855914/1` (queue outlier) | 762 s | 314 s | 377 s | 17 s |

`45207855914` took about 24½ minutes end to end, driven by queueing rather
than a slow test job. `45714857681` took 13:24 from creation through rerun
completion. Neither can be confirmed as the owner's specific ~15-minute
observation. Recent successful jobs paid almost no queue cost. On the exact
baseline run, workflow timings were 4 s for venv setup, 12 s application/dev
dependencies, 4 s Playwright/Chromium, 3 s PDF.js, 222 s pytest, 102 s admin
Chromium, 69 s reader Chromium, and 19 s for the report job (14 s mailbox
step). `45914480572` recorded the same install costs with 207 s pytest,
89 s admin Chromium and 68 s reader Chromium. These measurements favor
removing repeated work over caching or adding parallel workers on the single
trusted runner. Existing superseded-run cancellation and the reporting job
remain; no extra status job or runner capacity is required. The local pytest
saving alone does not establish a 4–5 minute hosted workflow.
