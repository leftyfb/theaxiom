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
