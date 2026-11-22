"""Repository license and package metadata consistency checks."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_apache_license_and_project_metadata():
    license_text = (ROOT / "LICENSE").read_text()
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "APPENDIX: How to apply the Apache License to your work." in license_text
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert metadata["license"] == "Apache-2.0"
    assert "Programming Language :: Python :: 3" in metadata["classifiers"]


def test_license_scope_and_notice_are_documented():
    readme = (ROOT / "README.md").read_text()
    notice = (ROOT / "NOTICE").read_text()
    assert "## License" in readme
    assert "Apache License, Version 2.0" in readme
    assert "does not apply" in readme
    assert "third-party" in notice.lower()
    assert "Copyright 2026" in notice
