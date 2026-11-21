"""Guard the machine-readable dependency inventory against silent drift (#79)."""

from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_inventory_is_parseable_and_uses_canonical_python_manifest():
    inventory = yaml.safe_load((ROOT / "docs/dependencies.yaml").read_text(encoding="utf-8"))
    assert inventory["schema_version"] == 1
    assert inventory["python"]["manifest"] == "pyproject.toml"
    assert inventory["python"]["runtime"] is True
    assert inventory["python"]["dev_extra"] == "dev"
    assert inventory["targets"]["ubuntu"] == ["24.04", "26.04"]
    assert set(inventory["targets"]["architectures"]) == {"amd64", "arm64"}
    assert inventory["targets"]["qualification"] == "pending"
    assert (ROOT / inventory["python"]["manifest"]).is_file()


def test_apt_inventory_groups_have_no_duplicates_or_invalid_package_names():
    import re

    inventory = yaml.safe_load((ROOT / "docs/dependencies.yaml").read_text(encoding="utf-8"))
    for group, packages in inventory["apt"].items():
        assert isinstance(packages, list), group
        assert len(packages) == len(set(packages)), group
        for package in packages:
            assert isinstance(package, str) and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package), (group, package)


def test_python_manifest_has_the_documented_runtime_and_dev_dependencies():
    # tomllib is standard only on Python 3.11+. Runtime still supports 3.10;
    # this inventory check runs on CI's Python 3.12, not during installation.
    if sys.version_info < (3, 11):
        pytest.skip("requires stdlib tomllib (Python 3.11+)")
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.10"
    assert {"jinja2", "pyyaml", "pypdf", "langdetect"} == {
        requirement.split(">=")[0].split("==")[0].lower()
        for requirement in project["dependencies"]
    }
    dev = project["optional-dependencies"]["dev"]
    assert {"pytest", "pytest-cov", "ruff"} == {
        requirement.split(">=")[0].split("==")[0].lower() for requirement in dev
    }
