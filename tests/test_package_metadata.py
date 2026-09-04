import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_package_and_plugin_manifest_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = json.loads((ROOT / "openhop-plugin.json").read_text(encoding="utf-8"))

    assert project["version"] == manifest["version"]


def test_openhop_core_dependency_uses_exact_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "openhop-core==1.1.1" in project["dependencies"]
