import json
from pathlib import Path

import pytest

from meshcore_nomad_bridge.config import ConfigError, Settings


_ENV_KEYS = (
    "OPENHOP_PLUGIN_DATA",
    "MESHCORE_HOST",
    "MESHCORE_PORT",
    "NOMAD_URL",
    "NOMAD_MODEL",
    "NOMAD_COLLECTION",
    "NOMAD_TIMEOUT_SECONDS",
    "ONE_SHOT",
    "NOMAD_SESSION_MAP_PATH",
    "MAX_CONCURRENT_REQUESTS",
    "NOMAD_BUSY_WAIT_SECONDS",
    "MAX_REPLY_CHUNKS",
    "MAX_CHUNK_BYTES",
    "MAX_PROMPT_BYTES",
    "RADIO_PROMPT_ENABLED",
    "RADIO_PROMPT_TEMPLATE",
    "DUPLICATE_TTL_SECONDS",
    "LOG_LEVEL",
)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_config(path: Path, **values: object) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps(values), encoding="utf-8")


def test_settings_load_plugin_owned_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(
        data_dir,
        meshcore_host="127.0.0.2",
        meshcore_port=5056,
        nomad_url="http://nomad.local:8080",
        nomad_model="qwen-test",
        one_shot=False,
        max_reply_chunks=3,
    )
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    settings = Settings.from_env()

    assert settings.meshcore_host == "127.0.0.2"
    assert settings.meshcore_port == 5056
    assert settings.nomad_url == "http://nomad.local:8080"
    assert settings.nomad_model == "qwen-test"
    assert settings.one_shot is False
    assert settings.max_reply_chunks == 3


def test_environment_overrides_plugin_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(
        data_dir,
        nomad_url="http://from-file:8080",
        nomad_model="file-model",
        meshcore_port=5001,
    )
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("NOMAD_URL", "http://from-env:8080")
    monkeypatch.setenv("NOMAD_MODEL", "env-model")
    monkeypatch.setenv("MESHCORE_PORT", "6001")

    settings = Settings.from_env()

    assert settings.nomad_url == "http://from-env:8080"
    assert settings.nomad_model == "env-model"
    assert settings.meshcore_port == 6001


def test_session_map_defaults_to_plugin_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(data_dir, nomad_url="http://nomad.local", nomad_model="test-model")
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    settings = Settings.from_env()

    assert Path(settings.nomad_session_map_path) == data_dir / "nomad_sessions.json"


def test_standalone_session_map_default_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://nomad.local")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")

    settings = Settings.from_env()

    assert settings.nomad_session_map_path == "./data/nomad_sessions.json"


def test_invalid_plugin_config_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    with pytest.raises(ConfigError, match="config.json"):
        Settings.from_env()
