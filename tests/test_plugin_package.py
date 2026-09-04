import json
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict[str, object]:
    return json.loads((ROOT / "openhop-plugin.json").read_text(encoding="utf-8"))


def _project() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_manifest_declares_simple_python_service_plugin() -> None:
    manifest = _manifest()

    assert manifest["schema"] == 1
    assert manifest["id"] == "openhop.nomad"
    assert manifest["name"] == "NOMAD Bridge"
    assert manifest["runtime"] == {
        "type": "python",
        "entrypoint": "meshcore-nomad-bridge",
    }
    assert "ui" not in manifest
    assert "permissions" not in manifest


def test_manifest_and_python_project_versions_match() -> None:
    manifest = _manifest()
    project = _project()

    assert project["name"] == "openhop-nomad-plugin"
    assert manifest["version"] == project["version"]


def test_plugin_distribution_stays_focused() -> None:
    assert not (ROOT / "Dockerfile").exists()
    assert not (ROOT / "docker-compose.yml").exists()
    assert not (ROOT / "meshcore-nomad-bridge.service").exists()
    assert not (ROOT / "nomad").exists()


def test_plugin_readme_documents_runtime_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "OPENHOP_PLUGIN_DATA" in readme
    assert "Companion frame server" in readme
    assert "config.json" in readme
