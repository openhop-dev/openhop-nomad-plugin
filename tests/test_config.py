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
    "MAX_PENDING_REQUESTS",
    "MAX_REQUESTS_PER_SENDER",
    "MAX_REQUESTS_GLOBAL",
    "RATE_LIMIT_WINDOW_SECONDS",
    "ALLOWED_SENDER_PREFIXES",
    "NOMAD_BUSY_WAIT_SECONDS",
    "MAX_REPLY_CHUNKS",
    "MAX_CHUNK_BYTES",
    "REPLY_CHUNK_DELAY_SECONDS",
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


@pytest.mark.parametrize("source", ["config.default.json", "openhop-plugin.json"])
def test_packaged_defaults_use_nomad_docker_endpoint(monkeypatch, tmp_path, source):
    _clean_env(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    defaults = json.loads((root / source).read_text(encoding="utf-8"))
    if source == "openhop-plugin.json":
        defaults = defaults["config"]["defaults"]
    _write_config(tmp_path, **defaults)
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(tmp_path))

    settings = Settings.from_env()

    assert settings.nomad_url == "http://nomad_admin:8080"
    assert (settings.meshcore_host, settings.meshcore_port) == ("127.0.0.1", 5050)


@pytest.mark.parametrize("source", ["ui/app.js", "ui/index.html"])
def test_ui_endpoint_defaults_match_docker_install(source):
    root = Path(__file__).resolve().parents[1]
    content = (root / source).read_text(encoding="utf-8")
    assert "http://nomad_admin:8080" in content
    assert "http://127.0.0.1:8080" not in content


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "https://remote.example:8443"])
@pytest.mark.parametrize("override", [None, "http://other.example:8080"])
def test_existing_endpoint_is_preserved_without_rewriting_config(
    monkeypatch, tmp_path, url, override
):
    _clean_env(monkeypatch)
    _write_config(tmp_path, nomad_url=url, nomad_model="existing-model")
    config_path = tmp_path / "config.json"
    before = config_path.read_bytes()
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(tmp_path))
    if override:
        monkeypatch.setenv("NOMAD_URL", override)

    assert Settings.from_env().nomad_url == (override or url)
    assert config_path.read_bytes() == before


def test_builtin_companion_port_defaults_to_5050(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://nomad_admin:8080")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")
    assert Settings.from_env().meshcore_port == 5050


@pytest.mark.parametrize("override", [None, "6001"])
def test_existing_companion_port_is_preserved(monkeypatch, tmp_path, override):
    _clean_env(monkeypatch)
    _write_config(
        tmp_path, nomad_url="http://nomad_admin:8080", nomad_model="test-model", meshcore_port=5001
    )
    config_path = tmp_path / "config.json"
    before = config_path.read_bytes()
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(tmp_path))
    if override:
        monkeypatch.setenv("MESHCORE_PORT", override)
    assert Settings.from_env().meshcore_port == (int(override) if override else 5001)
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    "source, expected",
    [
        ("ui/app.js", "meshcore_port: 5050"),
        ("ui/index.html", "127.0.0.1:5050"),
    ],
)
def test_ui_companion_port_defaults_to_5050(source, expected):
    root = Path(__file__).resolve().parents[1]
    assert expected in (root / source).read_text(encoding="utf-8")


def test_settings_load_plugin_owned_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(
        data_dir,
        meshcore_host="127.0.0.2",
        meshcore_port=5056,
        nomad_url="http://10.5.30.7:8080",
        nomad_model="qwen-test",
        one_shot=True,
        max_reply_chunks=3,
        reply_chunk_delay_seconds=2.5,
        max_pending_requests=3,
        max_requests_per_sender=2,
        max_requests_global=7,
        rate_limit_window_seconds=45,
        allowed_sender_prefixes=["010203040506", "aabbccddeeff"],
    )
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    settings = Settings.from_env()

    assert settings.meshcore_host == "127.0.0.2"
    assert settings.meshcore_port == 5056
    assert settings.nomad_url == "http://10.5.30.7:8080"
    assert settings.nomad_model == "qwen-test"
    assert settings.one_shot is True
    assert settings.max_reply_chunks == 3
    assert settings.reply_chunk_delay_seconds == 2.5
    assert settings.max_pending_requests == 3
    assert settings.max_requests_per_sender == 2
    assert settings.max_requests_global == 7
    assert settings.rate_limit_window_seconds == 45
    assert settings.allowed_sender_prefixes == ("010203040506", "aabbccddeeff")


def test_environment_overrides_plugin_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(
        data_dir,
        nomad_url="http://10.5.30.8:8080",
        nomad_model="file-model",
        meshcore_port=5001,
    )
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.9:8080")
    monkeypatch.setenv("NOMAD_MODEL", "env-model")
    monkeypatch.setenv("MESHCORE_PORT", "6001")

    settings = Settings.from_env()

    assert settings.nomad_url == "http://10.5.30.9:8080"
    assert settings.nomad_model == "env-model"
    assert settings.meshcore_port == 6001


def test_session_map_defaults_to_plugin_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(data_dir, nomad_url="http://10.5.30.7", nomad_model="test-model")
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    settings = Settings.from_env()

    assert Path(settings.nomad_session_map_path) == data_dir / "nomad_sessions.json"


def test_standalone_session_map_default_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.7")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")

    settings = Settings.from_env()

    assert settings.nomad_session_map_path == "./data/nomad_sessions.json"


def test_persistent_mode_is_rejected_even_with_sender_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    _write_config(
        data_dir,
        nomad_url="http://10.5.30.7",
        nomad_model="test-model",
        one_shot=False,
        allowed_sender_prefixes=["010203040506"],
    )
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    with pytest.raises(ConfigError, match="ONE_SHOT"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NOMAD_TIMEOUT_SECONDS", "nan"),
        ("NOMAD_TIMEOUT_SECONDS", "inf"),
        ("RATE_LIMIT_WINDOW_SECONDS", "nan"),
        ("NOMAD_BUSY_WAIT_SECONDS", "-inf"),
        ("REPLY_CHUNK_DELAY_SECONDS", "inf"),
    ],
)
def test_nonfinite_numeric_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.7:8080")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match="finite"):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://10.5.30.7:bad",
        "http://10.5.30.7:70000",
        "http://10.5.30.7/base",
        "http://10.5.30.7/?query=1",
    ],
)
def test_nomad_url_rejects_invalid_origin(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", url)
    monkeypatch.setenv("NOMAD_MODEL", "test-model")

    with pytest.raises(ConfigError, match="NOMAD_URL"):
        Settings.from_env()


@pytest.mark.parametrize("template", ["{question} {missing}", "{{question}}"])
def test_radio_prompt_template_rejects_invalid_fields(
    monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.7:8080")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")
    monkeypatch.setenv("RADIO_PROMPT_TEMPLATE", template)

    with pytest.raises(ConfigError, match="RADIO_PROMPT_TEMPLATE"):
        Settings.from_env()


def test_environment_parses_comma_separated_sender_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.7")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")
    monkeypatch.setenv("ALLOWED_SENDER_PREFIXES", "010203040506, AABBCCDDEEFF")

    settings = Settings.from_env()

    assert settings.allowed_sender_prefixes == ("010203040506", "aabbccddeeff")


def test_invalid_sender_allowlist_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", "http://10.5.30.7")
    monkeypatch.setenv("NOMAD_MODEL", "test-model")
    monkeypatch.setenv("ALLOWED_SENDER_PREFIXES", "not-hex")

    with pytest.raises(ConfigError, match="12 hexadecimal"):
        Settings.from_env()


def test_invalid_plugin_config_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(data_dir))

    with pytest.raises(ConfigError, match="config.json"):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://nomad.local:8080",
        "http://nomad_admin:8080",
        "https://localhost",
        "http://[::1]:8080",
    ],
)
def test_nomad_url_accepts_hostnames_and_ip_literals(monkeypatch, url):
    _clean_env(monkeypatch)
    monkeypatch.setenv("NOMAD_URL", url)
    monkeypatch.setenv("NOMAD_MODEL", "test")
    assert Settings.from_env().nomad_url == url
