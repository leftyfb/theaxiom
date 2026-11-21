# Browser-driven regression testing

`tests/ci_test_browser.py` runs a real Chromium browser against The Axiom's
actual local HTTP handler. It uses an ephemeral source catalog and administrator
credential file created inside a temporary directory, and binds to an assigned
127.0.0.1 port. It does **not** connect to Vancouver, touch NFS archives,
start downloads, publish sources, or use production credentials.

## Continuous integration

The `Application CI` workflow runs this check after the existing Python,
Kiwix, and Nginx tests. It installs Playwright and Chromium and exercises
both 1366×900 desktop and 390×844 mobile viewports. A failed run uploads
screenshots and uncaught JavaScript errors as the
`browser-regression-failures` workflow artifact.

Current browser scenarios:

- Public homepage and search navigation
- Administrator sign-in boundary, incorrect password and valid login
- Administrator guide modal and expandable filter panel
- Creating, finding, editing and saving an isolated catalog source
- Filter behavior with matching and non-matching source names
- Source bulk-selection toggle behavior, without carrying out a bulk action
- Groupings and Settings navigation
- Horizontal overflow checks on the admin pages at both viewport widths
- Logout and session invalidation
- Uncaught browser JavaScript errors

These are **behavioral regression checks**, not screenshot-golden tests.
Intentional visual redesigns need not invalidate tests unless controls break,
disappear, overflow or become inaccessible.

## Running locally

On a development machine (never on the production instance):

```sh
python -m pip install -e '.[dev]'
python -m pip install 'playwright>=1.50,<2'
python -m playwright install chromium
python tests/ci_test_browser.py
```

Playwright downloads its own test browser; it does not download ZIM or document
archives. The test harness starts and closes its own loopback-only server.

## Boundaries and expansion

Chromium browser tests complement (do not replace) the independent real Nginx
publication/authentication tests, Kiwix fixture smoke test, and application
unit tests. They currently do not test production-specific Tailscale routing,
1Password browser integration, accessibility with a screen reader, actual
network interruptions, large downloads, or the full offline PDF/EPUB reader.

Next browser coverage should use isolated small PDF, EPUB and ZIM fixtures to
exercise reader navigation and search, source publication controls and role
boundaries, grouping assignment and bulk actions, responsive layout at more
breakpoints, keyboard-only operation and accessibility audits. Never use live
Wikipedia or Ready.gov archives to generate browser test conditions.

## Closed-issue regression coverage audit (2026-11-21)

Reviewed **all 35 closed issues** as of this audit. "Partial" means a related
behavior has browser coverage, not that every acceptance criterion is proven.
"Unit/CI" means the behavior is covered by existing non-browser tests. This
matrix must not be mistaken for owner acceptance or a live production test.

| Closed issues | Regression contract | Current verification |
| --- | --- | --- |
| #1, #65, #82, #85 | Compact, initially collapsed source cards; contextual actions; avoid obsolete technical verification labels | Partial browser: source summary expand/edit and mobile overflow; template unit assertions |
| #3, #68 | Add, edit, validate, cancel, remove source profiles and persist across reloads | Browser: create, edit, metadata auto-populate, cancel, confirm/dismiss remove, persistence and mobile layout |
| #6 | Public library displays only eligible verified and published content | Unit/CI: publication gate; partial browser: public navigation |
| #13, #67 | Eight-character password policy, branded login/favicon, session logout | Unit/CI for password policy and icons; browser for login, incorrect password, logout; external 1Password remains owner-only |
| #15 | Separate Sources, Groupings and Settings routes with correct scripts | Browser navigation and page errors; template assertions prohibit Settings script on Groupings |
| #16, #44, #90–92, #97 | Shared Kiwix, UUID book identity, reader navigation/back, native search | Real isolated Kiwix CI fixture and unit tests; full native browser article/search coverage still pending |
| #45 | Public homepage compact mobile layout, no overflow, navigable Getting Started | Partial browser: public home/search; further responsive tests needed |
| #69 | Discoverable offline content source guide | Browser modal open/dismiss and static template tests |
| #70, #75, #77 | PDF/EPUB URLs, local upload, metadata and language, reader navigation | Browser: filename-derived title/format only; fixture-based upload/reader journeys pending |
| #71, #76, #78 | Group management, selection/bulk safety, publication independent filters, sorting, automatic unpublish | Browser: create/rename/delete grouping, filter/reset/ordering, bulk-selection gating and safe catalog removal; actual eligible publish/unpublish with small fixtures pending |
| #73 | Correct Bug-In Guide authorship/source choice | Catalog metadata/source unit validation; no live download |
| #74, #80, #81, #84 | Embedded PDF.js controls, search/matches, mobile toolbar | Unit/DOM tests; browser with disposable multi-page searchable PDF fixture pending |
| #83 | EPUB chapters, TOC, text size, keyboard navigation, return to library | Unit tests; browser with disposable EPUB fixture pending |
| #86 | Pause/resume/cancel source-scoped ZIM download | Unit/CI state-machine tests; browser transport simulation with small disposable ZIM pending |
| #89 | LAN and Tailscale Host syntax, login and publication isolation | Unit/CI Host validation and real Nginx auth smoke; network-path test remains deployment-specific |
| #96 | User-accepted mobile checklist | Retain historical owner acceptance; never auto-check or alter its completed boxes |

The browser suite is a **regression layer**: it tests observable controls, navigation,
accessibility basics and safety boundaries, while non-browser tests verify file
integrity and acquisition state machines. No regression run should manufacture a
failed production download, recheck the installed Wikipedia/Ready.gov archives,
or rely on the owner's external password manager.

Highest-impact next phases:

1. Use **tiny synthetic PDF and EPUB documents** in isolated fixture storage to
   cover reader toolbar, EPUB chapter navigation, bookmarks, PDF text search,
   safe unpublished denial and publication changes.
2. Exercise multiple disposable source profiles and grouping memberships in
   Chromium, including bulk add/remove, active/busy controls and permissions.
3. Add keyboard-only navigation checks, automated accessibility audits, and
   additional small/large viewport screenshot comparisons on controlled fixtures.
4. Extend isolated native Kiwix fixture smoke through Chromium for actual
   search and article navigation (no full Wikipedia/FEMA).
