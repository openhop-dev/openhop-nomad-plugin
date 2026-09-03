"""Configuration for the openHop NOMAD plugin and standalone bridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


_CONFIG_KEY_OVERRIDES = {
    "NOMAD_BUSY_WAIT_SECONDS": "busy_wait_seconds",
}


@dataclass(frozen=True)
class Settings:
    meshcore_host: str
    meshcore_port: int

    nomad_url: str
    nomad_model: str
    nomad_collection: str | None
    nomad_timeout_seconds: float
    one_shot: bool
    nomad_session_map_path: str

    max_concurrent_requests: int
    busy_wait_seconds: float

    max_reply_chunks: int
    max_chunk_bytes: int
    max_prompt_bytes: int

    radio_prompt_enabled: bool
    radio_prompt_template: str

    duplicate_ttl_seconds: int

    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        plugin_data_dir, config = _load_plugin_config()

        meshcore_host = _get_str("MESHCORE_HOST", "127.0.0.1", config)
        meshcore_port = _get_int("MESHCORE_PORT", 5001, config)

        nomad_url = _get_required_str("NOMAD_URL", config)
        nomad_model = _get_required_str("NOMAD_MODEL", config)
        nomad_collection = _get_optional_str("NOMAD_COLLECTION", config)
        nomad_timeout_seconds = _get_float("NOMAD_TIMEOUT_SECONDS", 120.0, config)
        one_shot = _get_bool("ONE_SHOT", True, config)
        session_map_default = (
            str(plugin_data_dir / "nomad_sessions.json")
            if plugin_data_dir is not None
            else "./data/nomad_sessions.json"
        )
        nomad_session_map_path = _get_str(
            "NOMAD_SESSION_MAP_PATH", session_map_default, config
        )

        max_concurrent_requests = _get_int("MAX_CONCURRENT_REQUESTS", 2, config)
        busy_wait_seconds = _get_float("NOMAD_BUSY_WAIT_SECONDS", 5.0, config)

        max_reply_chunks = _get_int("MAX_REPLY_CHUNKS", 4, config)
        max_chunk_bytes = _get_int("MAX_CHUNK_BYTES", 145, config)
        max_prompt_bytes = _get_int("MAX_PROMPT_BYTES", 1000, config)

        radio_prompt_enabled = _get_bool("RADIO_PROMPT_ENABLED", True, config)
        radio_prompt_template = _get_str(
            "RADIO_PROMPT_TEMPLATE",
            "You are answering a question received over a low-bandwidth MeshCore radio network.\\n"
            "Give the most useful answer first.\\n"
            "Be concise.\\n"
            "Use plain text.\\n"
            "Do not use Markdown tables.\\n"
            "Avoid unnecessary introductions.\\n"
            "Aim for fewer than 400 characters when practical.\\n\\n"
            "User question:\\n{question}",
            config,
        )

        duplicate_ttl_seconds = _get_int("DUPLICATE_TTL_SECONDS", 600, config)

        log_level = _get_str("LOG_LEVEL", "INFO", config).upper()

        settings = cls(
            meshcore_host=meshcore_host,
            meshcore_port=meshcore_port,
            nomad_url=nomad_url,
            nomad_model=nomad_model,
            nomad_collection=nomad_collection,
            nomad_timeout_seconds=nomad_timeout_seconds,
            one_shot=one_shot,
            nomad_session_map_path=nomad_session_map_path,
            max_concurrent_requests=max_concurrent_requests,
            busy_wait_seconds=busy_wait_seconds,
            max_reply_chunks=max_reply_chunks,
            max_chunk_bytes=max_chunk_bytes,
            max_prompt_bytes=max_prompt_bytes,
            radio_prompt_enabled=radio_prompt_enabled,
            radio_prompt_template=radio_prompt_template,
            duplicate_ttl_seconds=duplicate_ttl_seconds,
            log_level=log_level,
        )
        _validate(settings)
        return settings


def _load_plugin_config() -> tuple[Path | None, dict[str, Any]]:
    raw_data_dir = os.getenv("OPENHOP_PLUGIN_DATA", "").strip()
    if not raw_data_dir:
        return None, {}

    data_dir = Path(raw_data_dir).expanduser()
    config_path = data_dir / "config.json"
    if not config_path.exists():
        return data_dir, {}

    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to read plugin config.json: {exc}") from exc

    if not isinstance(value, dict):
        raise ConfigError("Plugin config.json must contain a JSON object")

    return data_dir, value


def _config_key(name: str) -> str:
    return _CONFIG_KEY_OVERRIDES.get(name, name.lower())


def _raw_value(name: str, config: dict[str, Any], default: Any = None) -> Any:
    raw = os.getenv(name)
    if raw is not None and raw.strip() != "":
        return raw
    return config.get(_config_key(name), default)


def _get_required_str(name: str, config: dict[str, Any]) -> str:
    value = _raw_value(name, config, "")
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _get_optional_str(name: str, config: dict[str, Any]) -> str | None:
    value = _raw_value(name, config)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    value = value.strip()
    return value or None


def _get_str(name: str, default: str, config: dict[str, Any]) -> str:
    value = _raw_value(name, config, default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value.strip()


def _get_int(name: str, default: int, config: dict[str, Any]) -> int:
    raw = _raw_value(name, config, default)
    if isinstance(raw, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _get_float(name: str, default: float, config: dict[str, Any]) -> float:
    raw = _raw_value(name, config, default)
    if isinstance(raw, bool):
        raise ConfigError(f"{name} must be a number")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _get_bool(name: str, default: bool, config: dict[str, Any]) -> bool:
    raw = _raw_value(name, config, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in {0, 1}:
        return bool(raw)
    if not isinstance(raw, str):
        raise ConfigError(f"{name} must be a boolean")
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def _validate(settings: Settings) -> None:
    if not settings.meshcore_host:
        raise ConfigError("MESHCORE_HOST must not be empty")
    if not (1 <= settings.meshcore_port <= 65535):
        raise ConfigError("MESHCORE_PORT must be between 1 and 65535")

    if not settings.nomad_url.startswith(("http://", "https://")):
        raise ConfigError("NOMAD_URL must start with http:// or https://")
    if not settings.nomad_session_map_path:
        raise ConfigError("NOMAD_SESSION_MAP_PATH must not be empty")

    if settings.nomad_timeout_seconds <= 0:
        raise ConfigError("NOMAD_TIMEOUT_SECONDS must be > 0")
    if settings.max_concurrent_requests <= 0:
        raise ConfigError("MAX_CONCURRENT_REQUESTS must be > 0")
    if settings.busy_wait_seconds < 0:
        raise ConfigError("NOMAD_BUSY_WAIT_SECONDS must be >= 0")

    if settings.max_reply_chunks <= 0:
        raise ConfigError("MAX_REPLY_CHUNKS must be > 0")
    if settings.max_chunk_bytes < 40:
        raise ConfigError("MAX_CHUNK_BYTES must be >= 40")
    if settings.max_prompt_bytes <= 0:
        raise ConfigError("MAX_PROMPT_BYTES must be > 0")

    if "{question}" not in settings.radio_prompt_template:
        raise ConfigError("RADIO_PROMPT_TEMPLATE must contain {question}")

    if settings.duplicate_ttl_seconds < 60:
        raise ConfigError("DUPLICATE_TTL_SECONDS must be >= 60")
