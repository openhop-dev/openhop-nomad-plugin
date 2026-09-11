"""Configuration for the openHop NOMAD plugin and standalone bridge."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlsplit


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
    max_pending_requests: int
    max_requests_per_sender: int
    max_requests_global: int
    rate_limit_window_seconds: float
    allowed_sender_prefixes: tuple[str, ...]
    busy_wait_seconds: float

    max_reply_chunks: int
    max_chunk_bytes: int
    reply_chunk_delay_seconds: float
    max_prompt_bytes: int

    radio_prompt_enabled: bool
    radio_prompt_template: str

    duplicate_ttl_seconds: int

    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        plugin_data_dir, config = _load_plugin_config()

        meshcore_host = _get_str("MESHCORE_HOST", "127.0.0.1", config)
        meshcore_port = _get_int("MESHCORE_PORT", 5050, config)

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

        max_concurrent_requests = _get_int("MAX_CONCURRENT_REQUESTS", 1, config)
        max_pending_requests = _get_int("MAX_PENDING_REQUESTS", 1, config)
        max_requests_per_sender = _get_int("MAX_REQUESTS_PER_SENDER", 2, config)
        max_requests_global = _get_int("MAX_REQUESTS_GLOBAL", 4, config)
        rate_limit_window_seconds = _get_float("RATE_LIMIT_WINDOW_SECONDS", 60.0, config)
        allowed_sender_prefixes = _get_sender_prefixes("ALLOWED_SENDER_PREFIXES", config)
        busy_wait_seconds = _get_float("NOMAD_BUSY_WAIT_SECONDS", 5.0, config)

        max_reply_chunks = _get_int("MAX_REPLY_CHUNKS", 4, config)
        max_chunk_bytes = _get_int("MAX_CHUNK_BYTES", 80, config)
        reply_chunk_delay_seconds = _get_float("REPLY_CHUNK_DELAY_SECONDS", 2.0, config)
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
            max_pending_requests=max_pending_requests,
            max_requests_per_sender=max_requests_per_sender,
            max_requests_global=max_requests_global,
            rate_limit_window_seconds=rate_limit_window_seconds,
            allowed_sender_prefixes=allowed_sender_prefixes,
            busy_wait_seconds=busy_wait_seconds,
            max_reply_chunks=max_reply_chunks,
            max_chunk_bytes=max_chunk_bytes,
            reply_chunk_delay_seconds=reply_chunk_delay_seconds,
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


def _get_sender_prefixes(name: str, config: dict[str, Any]) -> tuple[str, ...]:
    raw = _raw_value(name, config, [])
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, list):
        values = raw
    else:
        raise ConfigError(f"{name} must be a list or comma-separated string")

    prefixes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ConfigError(f"{name} entries must be strings")
        prefix = value.strip().lower()
        if not prefix:
            continue
        if len(prefix) != 12 or any(char not in "0123456789abcdef" for char in prefix):
            raise ConfigError(f"{name} entries must be 12 hexadecimal characters")
        prefixes.append(prefix)
    return tuple(dict.fromkeys(prefixes))


def _validate(settings: Settings) -> None:
    if not settings.meshcore_host:
        raise ConfigError("MESHCORE_HOST must not be empty")
    if not (1 <= settings.meshcore_port <= 65535):
        raise ConfigError("MESHCORE_PORT must be between 1 and 65535")

    try:
        parsed_url = urlsplit(settings.nomad_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError
        if any(ord(char) <= 32 or ord(char) == 127 for char in settings.nomad_url):
            raise ValueError
        port = parsed_url.port
        if (
            parsed_url.username
            or parsed_url.password
            or parsed_url.fragment
            or parsed_url.query
            or parsed_url.path not in {"", "/"}
        ):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("NOMAD_URL must be a valid HTTP(S) origin with a hostname or IP address") from exc
    if not settings.nomad_session_map_path:
        raise ConfigError("NOMAD_SESSION_MAP_PATH must not be empty")

    numeric_settings = {
        "NOMAD_TIMEOUT_SECONDS": settings.nomad_timeout_seconds,
        "RATE_LIMIT_WINDOW_SECONDS": settings.rate_limit_window_seconds,
        "NOMAD_BUSY_WAIT_SECONDS": settings.busy_wait_seconds,
        "REPLY_CHUNK_DELAY_SECONDS": settings.reply_chunk_delay_seconds,
    }
    for name, value in numeric_settings.items():
        if not math.isfinite(value):
            raise ConfigError(f"{name} must be finite")

    if settings.nomad_timeout_seconds <= 0:
        raise ConfigError("NOMAD_TIMEOUT_SECONDS must be > 0")
    if settings.max_concurrent_requests <= 0:
        raise ConfigError("MAX_CONCURRENT_REQUESTS must be > 0")
    if settings.max_pending_requests < settings.max_concurrent_requests:
        raise ConfigError("MAX_PENDING_REQUESTS must be >= MAX_CONCURRENT_REQUESTS")
    if settings.max_requests_per_sender <= 0:
        raise ConfigError("MAX_REQUESTS_PER_SENDER must be > 0")
    if settings.max_requests_global <= 0:
        raise ConfigError("MAX_REQUESTS_GLOBAL must be > 0")
    if settings.max_requests_per_sender > settings.max_requests_global:
        raise ConfigError("MAX_REQUESTS_PER_SENDER must be <= MAX_REQUESTS_GLOBAL")
    if settings.rate_limit_window_seconds <= 0:
        raise ConfigError("RATE_LIMIT_WINDOW_SECONDS must be > 0")
    if not settings.one_shot:
        raise ConfigError("ONE_SHOT must be true; persistent sessions are not bounded upstream")
    if settings.busy_wait_seconds < 0:
        raise ConfigError("NOMAD_BUSY_WAIT_SECONDS must be >= 0")

    if settings.max_reply_chunks <= 0:
        raise ConfigError("MAX_REPLY_CHUNKS must be > 0")
    if settings.max_chunk_bytes < 40:
        raise ConfigError("MAX_CHUNK_BYTES must be >= 40")
    if not 0 <= settings.reply_chunk_delay_seconds <= 60:
        raise ConfigError("REPLY_CHUNK_DELAY_SECONDS must be between 0 and 60")
    if settings.max_prompt_bytes <= 0:
        raise ConfigError("MAX_PROMPT_BYTES must be > 0")

    try:
        parsed_template = list(Formatter().parse(settings.radio_prompt_template))
    except ValueError as exc:
        raise ConfigError("RADIO_PROMPT_TEMPLATE must be a valid format string") from exc
    fields = [field_name for _, field_name, _, _ in parsed_template if field_name is not None]
    invalid_fields = [
        field_name
        for _, field_name, format_spec, conversion in parsed_template
        if field_name is not None
        and (format_spec or conversion or field_name != "question")
    ]
    if "question" not in fields or invalid_fields:
        raise ConfigError("RADIO_PROMPT_TEMPLATE must contain only the {question} field")

    if settings.duplicate_ttl_seconds < 60:
        raise ConfigError("DUPLICATE_TTL_SECONDS must be >= 60")
