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

## AC-301: request-to-validation profile

These six November 26–27, 2026 UTC requests are a historical sample, not an
experiment. The linked mailbox issues provide issue timestamps, label events,
and result comments; Actions provides exact-SHA workflow, attempt, job and
step timestamps. Durations below are seconds. `—` means not measured or not
reported, not zero.

| Request | Open → requested | Requested → running | Running → result comment | Review label → close | Open → close |
| --- | ---: | ---: | ---: | ---: | ---: |
| [AC-285 #1147](https://github.com/leftyfb/agent-coordination/issues/1147) | 1 | 32 | 973 | 331 | 1341 |
| [AC-287 #1150](https://github.com/leftyfb/agent-coordination/issues/1150) | 30 | 34 | 1150 | 345 | 1561 |
| [AC-290 #1154](https://github.com/leftyfb/agent-coordination/issues/1154) | 2 | 48 | 1086 | 26 | 1165 |
| [AC-292 #1157](https://github.com/leftyfb/agent-coordination/issues/1157) | 2 | 4 | 971 | 97 | 1077 |
| [AC-295 #1162](https://github.com/leftyfb/agent-coordination/issues/1162) | 2 | 15 | 929 | 82 | 1031 |
| [AC-300 #1168](https://github.com/leftyfb/agent-coordination/issues/1168) | 1 | 45 | 1078 | 232 | 1359 |
| Median | 2 | 33 | 1025.5 | 164.5 | 1253 |

`ac:requested` → `ac:running` is a poller pickup proxy, not time spent inside
the poller. Running → comment includes work, local checks, and any retries,
pushes or reporting in that interval; implementation-only time is unavailable
for every row. The comment → review-label transition took 2–4 s. Review label
→ close is a mailbox interval, not reviewer labor or proof of CI or deploy
completion. For #1168, the final `closed` event (04:41:07Z) is used; issue
metadata gives 04:41:06Z. CI can start before a request reports or finish
after it closes; do not sum all columns and CI as a serial critical path.

| AC | Targeted local pytest (passing) | Full local pytest (passing) | Local browser / smoke | Retry / push evidence |
| --- | --- | --- | --- | --- |
| 285 | 197 in 68.87 s | — | — | No repairs or new commit in direct verification; push timing — |
| 287 | — | 944 + 196 subtests; time — | PDF/EPUB not run locally (missing PDF.js); other timing — | Push auth failed; [AC-288 #1151](https://github.com/leftyfb/agent-coordination/issues/1151) pushed the same SHA without retesting. Retry pickup 215 s; initial result comment → CI creation 299 s; push command times — |
| 290 | 234 passing; narrow follow-up 3 passing; times — | 944 + 196 subtests; time — | Admin, maps and synthetic ZIM passed; PDF/EPUB not run; times — | System Python lacked pytest; existing venv worked. Push reported successful; command time — |
| 292 | Direct rerun: 172 in 71.73 s | — | — | No repairs or push in direct rerun. Exact-head CI failed; later repair used a different SHA |
| 295 | 117 passing; time — | 952 + 196 subtests; time — | Desktop Chromium passed; time — | Two fixture corrections (missing staging dir; global check already due); retry time —. Push reported successful; command time — |
| 300 | — | Profile 153.75 s wall; post-push rerun 952 + 196 subtests in 152.99 s (pytest footer) | 184.87 s wall across five profiled checks, including 177.12 s in the two browser scripts | CI creation 04:33:55Z; post-push recheck reported at 04:37:12Z while CI was still running. Push command time — |

For push timing, CI creation is the earliest available workflow marker, not
push start, end, duration, or networking time. For example, #1162 reported
20 s and #1168 reported 197 s after their workflow was created. Those gaps
do not measure Git push latency. Missing local test timings cannot be derived
by subtraction from the mailbox interval.

| AC / exact head (unambiguous Git prefix) | Push CI run / attempt | Tests queue / job | Pytest / admin Chromium / reader Chromium steps | Report queue / job | Run created → updated | Conclusion |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 285 / `08c63b66932f` | `45914480572/1` | 1 / 403 | 207 / 89 / 68 | 1 / 19 | 426 | success |
| 287 / `0254b952a9ee` | `45924783178/1` | 1 / 418 | 218 / 93 / 68 | 1 / 18 | 438 | success after push retry |
| 290 / `6fc9bb12f8ec` | `45927694246/1` | 2 / 434 | 230 / 95 / 69 | 1 / 17 | 455 | success |
| 292 / `1ae0e9328e9f` | `45931369853/1` | 1 / 244 | 220 (failed) / skipped / skipped | 1 / 18 | 265 | failure |
| 295 / `f624b0bae096` | `45936719385/1` | 0 / 433 | 222 / 102 / 69 | 1 / 19 | 454 | success |
| 300 / `f375fa745a3e` | `45940679473/1` | 1 / 355 | 156 / 96 / 65 | 1 / 17 | 377 | success |

Each run is for a push to `main`, with no CI reruns observed for these SHAs;
the successful `report` job for AC-292 did not mean tests passed. Successful
test jobs took a median 418 s (355–434 s); queue time was 0–2 s. A green
targeted local rerun on AC-292 did not catch the CI failure. The repair request
[#1159](https://github.com/leftyfb/agent-coordination/issues/1159) verified
`4d48d013d331` (full local pytest: 219.23 s), with a new CI report at 03:19:33Z,
1037 s after the failed run's update. This is a recovery interval, not retry
execution time or a passing result for the original SHA.

Deployment was separate from development. The observed `ac:running` →
timestamped deployment outcome intervals are handoff proxies, not command
durations: [#1149](https://github.com/leftyfb/agent-coordination/issues/1149)
22 s, [#1153](https://github.com/leftyfb/agent-coordination/issues/1153) 23 s,
and [#1156](https://github.com/leftyfb/agent-coordination/issues/1156) 23 s.
For AC-292, [#1161](https://github.com/leftyfb/agent-coordination/issues/1161)
was 21 s and deployed the repaired SHA, not the failed one. For AC-295,
[#1164](https://github.com/leftyfb/agent-coordination/issues/1164) reported
rollback after 22 s, then [#1167](https://github.com/leftyfb/agent-coordination/issues/1167)
reported a successful guarded retry after 19 s (562 s from first failure to
successful outcome, including pickup and intervening checks). No deployment
timing for AC-300 was established in the bounded mailbox read. Exact operator
review, authorization and deployment command durations are unavailable.

### Duplicate work and savings estimate

Four of six mailbox results report full local pytest for the same SHA later
validated in full CI (AC-287/290/295/300); at least three report running both
targeted and full local pytest or full pytest plus browser checks (AC-290,
295 and 300). The overlap is work, not necessarily wasted coverage: local
checks are useful for risky changes and can give feedback before a push.
AC-300 is the measured example: 153.75 s local pytest and 184.87 s local
integration/browser wall time versus 156 s in CI pytest and 161 s in the
two CI Chromium steps (plus CI Kiwix and nginx checks). Its 152.99 s
post-push pytest recheck also duplicated the CI pytest scope; the reported
timings overlap the workflow window, so that time cannot simply be subtracted
from push-to-green or request-to-close time.

AC-301 timed one representative fixture-only session regression module on
tux at the AC-300 SHA (Python 3.14.9, pytest 9.2.0). This measures a small
local check, not coverage specific to the Markdown change:

```sh
mkdir .cache/ac-301  # use a fresh directory for each sample
TMPDIR="$PWD/.cache/ac-301" PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/time -f 'targeted wall=%e exit=%x' \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=.cache/ac-301/pytest-targeted tests/test_admin_sessions.py
```

Four tests passed (exit 0) in 3.42 s wall (3.07 s pytest footer). Against
AC-300's 153.75 s full pytest wall sample, this is an estimated **150.33 s
less local execution (97.8%)** for a change that only needs that subset.
Against the 338.62 s local pytest + five-check bundle, the estimated reduction
is **335.20 s (99.0%)** if none of those integration checks need local
iteration. These are differences between measured samples with different
scopes and flags, not reduced CI time or measured end-to-end gains. Actual
savings depend on the relevant subset, which checks would otherwise run,
overlap with CI, retries, queueing and review. AC-301 did not rerun full local
pytest or browsers for this documentation-only change.

## Validation policy

1. **Low risk:** For docs, test-only changes that do not weaken safeguards,
   and narrowly scoped behavior changes with known coverage, check the diff
   and run the affected fixture-backed regressions before pushing. For pure
   prose, check changed links and examples; do not run an unrelated full suite
   just for a duplicate green result. Record the selected commands and why
   they cover the edited surface.
2. **High risk, debugging, or CI unavailable:** Run full local pytest and the
   relevant integration/browser checks with isolated fixtures. Examples include
   authentication, permissions or publication, storage/acquisition/rollback,
   shared UI/reader behavior, and dependency or workflow changes. Debug with
   the smallest reproducing fixture first, then run broader checks. A passing
   local result does not replace the deployment gate if CI is unavailable.
3. **Before deployment:** Require green full Application CI on the exact
   authorized deploy SHA, including pytest and all Kiwix, nginx and Chromium
   checks. Record the run ID, attempt, head SHA and conclusion; do not rely on
   a report-job success, older or superseding SHA, canceled run or targeted
   run. A new commit needs new exact-head evidence. Separately require operator
   authorization and the existing deployment safety checks. No change to CI
   jobs, test coverage, or runner isolation: untrusted PRs stay on hosted
   runners, off the persistent trusted runner and out of mailbox reporting.
