# Development-to-deployment acceptance register

**HOLD — NO DEPLOYMENT:** As of November 20, 2026, Ready.gov is still downloading on Vancouver. The operator has authorized **development, commits and GitHub Actions tests only**. Do not deploy, run the deployment poller, restart services, interrupt any active download, change Vancouver files, or change ownership or modes. Resume deployment planning **only after explicit confirmation that the Ready.gov download completed and separate permission to deploy**.

**Last confirmed Vancouver revision:** `9cde77ab3ac1a5fef2c5db6abb3d0a2207be4858`. **Developed-only** changes are in `main`, not Vancouver.

This is an operator acceptance checklist, not an automated deployment instruction. CI green means code acceptance, **not** live acceptance. Keep this register updated whenever a feature is committed while the hold is in place. Record the *specific deployed SHA* only after an authorized deployment.

| Issue / scope | Code status | Known commits / CI | Post-download acceptance on Vancouver |
| --- | --- | --- | --- |
| [#2](https://github.com/leftyfb/theaxiom/issues/2) generic ZIM publishing, multi-book reader and publication gates | Code-only tested; **live acceptance pending**, issue open | `3049cb800594`; [CI 44832207865](https://github.com/leftyfb/theaxiom/actions/runs/44832207865) — 382 tests + 75 subtests, real Kiwix and Nginx smoke | Verify Ready.gov completion before touching readers. Check installed Kiwix XML compatibility, install optional loopback 8082 reader **only with approval**, test a *separate small* ZIM, rights confirmation, published/unpublished deep links, search, multiple archives, automatic reload and Wikipedia on 8081 |
| [#86](https://github.com/leftyfb/theaxiom/issues/86) ZIM pause/resume/cancel | Code-only tested; **live acceptance pending**, issue open | `2e1731eb87d6`; [CI 44831371563](https://github.com/leftyfb/theaxiom/actions/runs/44831371563) | Test pause, resume, cancel and network error on a **new small test download**. Confirm saved ETag/range, partial retention, cleanup only on cancellation, completed files never deleted, persistence after restart; keep existing library and staging dirs at operator-selected **0777** |
| [#85](https://github.com/leftyfb/theaxiom/issues/85) remove internal installed-verification labels from Sources UI | Code accepted, issue closed; **visual acceptance pending** | `5042bfab66af`; [CI 44833153514](https://github.com/leftyfb/theaxiom/actions/runs/44833153514) — 386 tests + 75 subtests | Inspect PDF, EPUB and ZIM source cards on mobile/desktop; neither 'Installed verification' nor 'pin verified' appears; ordinary size/status and publication state still update |
| [#67](https://github.com/leftyfb/theaxiom/issues/67) branded admin sign-in and favicon / 1Password identity | **Code in development; live browser acceptance pending — DO NOT DEPLOY** | `aa0763168a2a` icons; `1c984e5da8ec` bounded session store; `3181f1116eb8` login/session integration; `79c4395fc64a` regression fixes; final exact-commit CI pending | Open public/admin/search/PDF/EPUB pages and verify SVG/PNG/ICO load offline without authentication prompts. Verify anonymous `/admin` redirects to branded `/admin/login`, 1Password recognizes `username` and `current-password` fields, save/fill produces a correctly named Login in Chrome, logout revokes session, expired sessions fail closed, Basic API clients still authenticate, CSRF rejects forged POST, same-origin checks remain, password rotation invalidates all browser sessions and old Basic credentials. Inspect Nginx scoped cookie forwarding (only `theaxiom_session`) and HTTPS Secure cookies. **Higher-risk auth change: require separate owner acceptance** |

## Mandatory gate before any future deployment

- [ ] Operator explicitly confirms Ready.gov is finished and verified; do not infer from a CI run or elapsed time
- [ ] Operator separately authorizes **which revision** to deploy and any new Kiwix/Nginx/systemd changes
- [ ] Compare deployment plan against actual installed 8081 Wikipedia reader and active/staged download state
- [ ] Preserve application UID/GID, source catalog, settings, rights flags and files; do **not** chmod/chown existing managed storage directories (including 0777)
- [ ] Validate `nginx -t` and systemd units, then execute the approved deployment
- [ ] Perform every per-issue live acceptance check above, record pass/fail and deployed SHA, and report any required rollback

Do not check these boxes during code-only work. An issue can be code-complete yet remain unaccepted on the actual target host.
