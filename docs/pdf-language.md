# PDF language detection (#75)

Language is inferred at acquisition only when the administrator has not supplied it.
The result is an editable metadata language code, not a security or publication
decision. Existing records are not automatically changed.

The old narrow handwritten PDF parser and six-language word list have been
replaced by pypdf (BSD-3-Clause; version 7.1.1) and langdetect (Apache-2.0;
version 1.0.9, bundled 55-language profiles). Both are declared in pyproject.toml.
See https://pypi.org/project/pypdf/ and https://pypi.org/project/langdetect/.
The classifier uses a fixed seed, a minimum sample, confidence threshold,
and multiple page/block samples. A credible Catalog /Lang declaration is used
unless it conflicts with extracted text. Ambiguous, empty and mixed samples may
remain unknown. OCR and password-protected documents are not supported.
Language recognition is probabilistic, particularly on short lists.

All parsing and inference takes place in an independent worker, with a five
second wall-clock timeout, four second CPU limit, 384 MiB address-space limit,
16 MiB input ceiling, 32 KiB sampled text ceiling, and at most eight sampled
pages. Only one worker is admitted per application process; simultaneous
requests skip inference instead of waiting. No network access is required
during normal operation. Worker failure, timeout or missing packages does
not reject otherwise verified content. No document text is logged.

## Existing deployment prerequisite

The fixed application deployment script does not authorize installing packages.
Before deploying this revision to an existing server, the operator must install
the declared versions into its existing application virtual environment and
verify imports as the service account. Installation requires package access or
an operator-prepared offline wheelhouse; do not install at runtime.

```sh
sudo /opt/theaxiom/app/.venv/bin/python -m pip install 'pypdf==7.1.1' 'langdetect==1.0.9'
sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -c 'import pypdf, langdetect; print(pypdf.__version__)'
```

The dependencies are pure-Python, subject to platform/Python-version tests,
and langdetect requires six. Track package preparation and upgrade policy in #79.
