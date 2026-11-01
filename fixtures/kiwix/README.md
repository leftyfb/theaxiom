# Tiny Kiwix-path fixture

`tiny.txt` is deterministic, synthetic repository-local content for the offline-library POC path. It was authored for this repository; it was not downloaded from Kiwix and contains no bulk or externally sourced content.

The fixture is intentionally much smaller than the role's 10 MiB POC bound. Verify its reproducible checksum and size with:

```text
python3 scripts/test_tiny_fixture.py
```

The test computes SHA-256 from the checked-in bytes and fails if the fixture is missing, changes unexpectedly, or exceeds the configured bound. No network access is used.
