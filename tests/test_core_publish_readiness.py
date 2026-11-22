"""Both PyPI projects (steuermaschine and steuermaschine-heinzel) are built and checked by the one release workflow."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_plugin_is_built_versioned_and_checked_like_the_core_package():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "uv version --package steuermaschine-heinzel" in workflow and "steuermaschine_heinzel" in workflow
    assert "--package steuermaschine-heinzel" in (ROOT / "Makefile").read_text()


def test_the_plugin_carries_the_metadata_pypi_needs():
    project = tomllib.loads((ROOT / "plugins/heinzel/pyproject.toml").read_text())["project"]
    assert project["name"] == "steuermaschine-heinzel" and project["license"] == "MIT" and project["urls"]["Source"]
    assert (ROOT / "plugins/heinzel" / project["readme"]).is_file() and (ROOT / "plugins/heinzel/LICENSE").is_file()
