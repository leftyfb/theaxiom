# Licensing and provenance

The original The Axiom application code, project templates, deployment examples,
and original documentation are distributed under Apache-2.0 (root `LICENSE`).
The `NOTICE` file identifies the project without asserting ownership of
unrelated content. Contributors must submit only code they own or are
authorized to license; existing third-party notices must be preserved.

## Repository audit (2026-11-22)

Reviewed the repository tree and declared dependency inventory. There is no
committed vendored PDF.js, Kiwix, ZIM, PDF or EPUB distribution, and no
bundled third-party Python dependency tree. The source tree contains original
Python application modules, Jinja templates, browser-side scripts, deployment
examples, tests and project documentation. Icons and binary favicon assets
should be checked for provenance before being reused outside this project;
this declaration does not override any independently applicable rights.

External dependencies are **not relicensed**. The application uses Jinja2,
PyYAML, pypdf and langdetect (plus transitive packages); Kiwix/zim-tools,
Nginx, and PDF.js are installed separately. Each retains its upstream license.
Consult `docs/dependencies.md` and upstream distributions for the exact
license text, notices and obligations for the versions shipped.

Library content is separate from the software: imported/downloaded ZIM,
PDF, EPUB, Wikipedia, Ready.gov, and other source materials retain their
individual rights and licenses. An operator must independently verify
redistribution rights and required attribution; The Axiom's Apache-2.0
license grants **no** rights in that material.

## Ownership and exceptions

The repository history and contribution provenance should be reviewed before
accepting outside contributions or asserting exclusive copyright ownership.
No contributor license agreement or third-party relicensing permission is
asserted here. Preserve any existing file-specific notices and investigate
unattributed assets or copied snippets before redistribution.
