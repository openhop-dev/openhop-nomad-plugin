"""Async client for Project N.O.M.A.D. chat API."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

MAX_HTTP_RESPONSE_BYTES = 262_144
MAX_HTTP_HEADER_BYTES = 65_536
HTTP_TOKEN_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
MAX_SESSION_HISTORY_MESSAGES = 20
MAX_SESSION_HISTORY_BYTES = 16_384


class NomadUnavailable(RuntimeError):
    """Raised when NOMAD cannot provide a valid response."""


HttpPost = Callable[..., Awaitable[tuple[int, str]]]
HttpRequest = Callable[..., Awaitable[tuple[int, str]]]


class NomadClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        collection: str | None,
        one_shot: bool = True,
        session_map_path: str = "./data/nomad_sessions.json",
        http_post: HttpPost | None = None,
        http_request: HttpRequest | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._collection = collection
        self._one_shot = one_shot
        self._session_map_path = Path(session_map_path)
        self._state_lock = asyncio.Lock()
        self._sender_locks: dict[str, asyncio.Lock] = {}

        if http_request is not None:
            self._http_request = http_request
        elif http_post is not None:
            self._http_request = _wrap_post_only(http_post)
        else:
            self._http_request = _default_http_request

        self._sender_sessions = self._load_session_map() if not one_shot else {}

    async def close(self) -> None:
        return None

    async def ask(self, prompt: str) -> str:
        return await self._chat(
            messages=[{"role": "user", "content": prompt}],
            session_id=None,
        )

    async def ask_for_sender(self, sender_id: str, prompt: str) -> str:
        if self._one_shot:
            return await self.ask(prompt)

        sender_lock = await self._get_sender_lock(sender_id)
        async with sender_lock:
            return await self._ask_persistent_for_sender(sender_id, prompt)

    async def _ask_persistent_for_sender(self, sender_id: str, prompt: str) -> str:
        session_id = await self._get_or_create_session_id(sender_id)
        try:
            history = await self._get_session_messages(session_id)
        except _NomadSessionNotFound:
            session_id = await self._create_fresh_session_for_sender(sender_id)
            history = []

        return await self._chat(
            messages=[*history, {"role": "user", "content": prompt}],
            session_id=session_id,
        )

    async def reset_session_for_sender(self, sender_id: str) -> int:
        if self._one_shot:
            raise NomadUnavailable("reset_not_supported_in_one_shot")
        sender_lock = await self._get_sender_lock(sender_id)
        async with sender_lock:
            return await self._create_fresh_session_for_sender(sender_id)

    async def _chat(
        self,
        *,
        messages: list[dict[str, str]],
        session_id: int | None,
    ) -> str:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "think": False,
        }
        if session_id is not None:
            payload["sessionId"] = session_id
        if self._collection:
            payload["collection"] = self._collection

        start = monotonic()

        try:
            status_code, body = await self._http_request(
                method="POST",
                url=f"{self._base_url}/api/ollama/chat",
                payload=payload,
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError as exc:
            logger.warning("NOMAD request timed out")
            raise NomadUnavailable("timeout") from exc
        except OSError as exc:
            logger.warning("NOMAD network error: %s", exc)
            raise NomadUnavailable("network") from exc

        elapsed = monotonic() - start
        logger.info("NOMAD completed in %.2fs", elapsed)

        if not 200 <= status_code < 300:
            logger.warning("NOMAD request failed with HTTP %s", status_code)
            raise NomadUnavailable(f"http_{status_code}")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("NOMAD response was not valid JSON")
            raise NomadUnavailable("invalid_json") from exc

        if not isinstance(data, dict):
            logger.warning("NOMAD response is not an object")
            raise NomadUnavailable("invalid_shape")

        message = data.get("message")
        if not isinstance(message, dict):
            logger.warning("NOMAD response missing message object")
            raise NomadUnavailable("missing_message")

        content = message.get("content")
        if not isinstance(content, str):
            logger.warning("NOMAD response missing message.content")
            raise NomadUnavailable("missing_content")

        content = content.strip()
        if not content:
            logger.warning("NOMAD response contained empty content")
            raise NomadUnavailable("empty_content")

        return content

    async def _get_or_create_session_id(self, sender_id: str) -> int:
        async with self._state_lock:
            existing = self._sender_sessions.get(sender_id)
        if existing is not None:
            return existing
        return await self._create_fresh_session_for_sender(sender_id)

    async def _get_sender_lock(self, sender_id: str) -> asyncio.Lock:
        async with self._state_lock:
            return self._sender_locks.setdefault(sender_id, asyncio.Lock())

    async def _create_fresh_session_for_sender(self, sender_id: str) -> int:
        payload: dict[str, object] = {
            "title": f"MeshCore {sender_id}",
            "model": self._model,
        }

        try:
            status_code, body = await self._http_request(
                method="POST",
                url=f"{self._base_url}/api/chat/sessions",
                payload=payload,
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError as exc:
            logger.warning("NOMAD create session request timed out")
            raise NomadUnavailable("timeout") from exc
        except OSError as exc:
            logger.warning("NOMAD network error while creating session: %s", exc)
            raise NomadUnavailable("network") from exc

        if not 200 <= status_code < 300:
            logger.warning("NOMAD create session failed with HTTP %s", status_code)
            raise NomadUnavailable(f"http_{status_code}")

        data = _parse_json_object(body)
        raw_id = data.get("id")
        try:
            session_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            logger.warning("NOMAD create session response missing numeric id")
            raise NomadUnavailable("invalid_session_id") from exc

        async with self._state_lock:
            self._sender_sessions[sender_id] = session_id
            self._save_session_map(self._sender_sessions)
        return session_id

    async def _get_session_messages(self, session_id: int) -> list[dict[str, str]]:
        try:
            status_code, body = await self._http_request(
                method="GET",
                url=f"{self._base_url}/api/chat/sessions/{session_id}",
                payload=None,
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError as exc:
            logger.warning("NOMAD get session request timed out")
            raise NomadUnavailable("timeout") from exc
        except OSError as exc:
            logger.warning("NOMAD network error while reading session: %s", exc)
            raise NomadUnavailable("network") from exc

        if status_code == 404:
            raise _NomadSessionNotFound(session_id)
        if not 200 <= status_code < 300:
            logger.warning("NOMAD get session failed with HTTP %s", status_code)
            raise NomadUnavailable(f"http_{status_code}")

        data = _parse_json_object(body)
        raw_messages = data.get("messages")
        if raw_messages is None:
            return []
        if not isinstance(raw_messages, list):
            logger.warning("NOMAD session response has invalid messages field")
            raise NomadUnavailable("invalid_session_messages")

        messages: list[dict[str, str]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in {"system", "user", "assistant"}:
                continue
            if not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})
        return _limit_session_history(messages)

    def _load_session_map(self) -> dict[str, int]:
        try:
            raw = self._session_map_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Failed to read session map %s: %s", self._session_map_path, exc)
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Session map file is invalid JSON: %s", self._session_map_path)
            return {}

        if not isinstance(data, dict):
            logger.warning("Session map file root must be an object: %s", self._session_map_path)
            return {}

        cleaned: dict[str, int] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                continue
        return cleaned

    def _save_session_map(self, mapping: dict[str, int]) -> None:
        try:
            self._session_map_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._session_map_path.with_suffix(self._session_map_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
            tmp_path.replace(self._session_map_path)
        except OSError as exc:
            logger.warning("Failed to persist session map %s: %s", self._session_map_path, exc)


class _NomadSessionNotFound(RuntimeError):
    def __init__(self, session_id: int) -> None:
        super().__init__(f"session_not_found:{session_id}")


def _parse_json_object(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning("NOMAD response was not valid JSON")
        raise NomadUnavailable("invalid_json") from exc
    if not isinstance(data, dict):
        logger.warning("NOMAD response is not an object")
        raise NomadUnavailable("invalid_shape")
    return data


def _limit_session_history(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    used_bytes = 0
    for message in reversed(messages):
        if len(selected) >= MAX_SESSION_HISTORY_MESSAGES:
            break
        message_bytes = len(message["role"].encode("utf-8")) + len(
            message["content"].encode("utf-8")
        )
        if used_bytes + message_bytes > MAX_SESSION_HISTORY_BYTES:
            break
        selected.append(message)
        used_bytes += message_bytes
    selected.reverse()
    return selected


def _wrap_post_only(http_post: HttpPost) -> HttpRequest:
    async def _request(
        *,
        method: str,
        url: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> tuple[int, str]:
        if method != "POST":
            raise OSError(f"Unsupported HTTP method via http_post adapter: {method}")
        if payload is None:
            payload = {}
        return await http_post(url=url, payload=payload, timeout_seconds=timeout_seconds)

    return _request


async def _default_http_request(
    *,
    method: str,
    url: str,
    payload: dict[str, object] | None,
    timeout_seconds: float,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        return await asyncio.wait_for(
            _async_http_request(method=method, url=url, body=body),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError("request_deadline_exceeded") from exc


async def _async_http_request(
    *,
    method: str,
    url: str,
    body: bytes | None,
) -> tuple[int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OSError("invalid_url")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise OSError("nomad_url_requires_ip_address") from exc

    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    writer: asyncio.StreamWriter | None = None
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        await asyncio.get_running_loop().sock_connect(sock, (str(address), port))
        ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
        reader, writer = await asyncio.open_connection(
            sock=sock,
            ssl=ssl_context,
            server_hostname=parsed.hostname if ssl_context else None,
        )
        sock = None
        request = _build_http_request(method=method, parsed=parsed, path=path, body=body)
        writer.write(request)
        await writer.drain()
        return await _read_async_http_response(reader, method)
    finally:
        if writer is not None:
            writer.transport.abort()
        elif sock is not None:
            sock.close()


def _build_http_request(*, method: str, parsed: Any, path: str, body: bytes | None) -> bytes:
    if method not in {"GET", "POST"}:
        raise OSError("unsupported_http_method")
    if not path or any(ord(char) < 33 or ord(char) > 126 for char in path):
        raise OSError("invalid_url")
    default_port = 443 if parsed.scheme == "https" else 80
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port and parsed.port != default_port:
        host = f"{host}:{parsed.port}"
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}",
        "Accept: application/json",
        "Connection: close",
    ]
    if body is not None:
        headers.extend(("Content-Type: application/json", f"Content-Length: {len(body)}"))
    try:
        return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + (body or b"")
    except UnicodeEncodeError as exc:
        raise OSError("invalid_url") from exc


async def _read_async_http_response(
    reader: asyncio.StreamReader, method: str
) -> tuple[int, str]:
    status_line = await _read_http_line(reader)
    if not status_line.endswith(b"\r\n"):
        raise OSError("invalid_http_response")
    parts = status_line[:-2].split(b" ", 2)
    try:
        version, raw_status = parts[0], parts[1]
        if version not in {b"HTTP/1.0", b"HTTP/1.1"}:
            raise ValueError
        if len(raw_status) != 3 or not raw_status.isdigit():
            raise ValueError
        if len(parts) == 3 and any(
            (byte < 32 and byte != 9) or byte == 127 for byte in parts[2]
        ):
            raise ValueError
        status = int(raw_status)
        if not 100 <= status <= 599:
            raise ValueError
    except (IndexError, ValueError) as exc:
        raise OSError("invalid_http_response") from exc

    header_bytes = len(status_line)
    headers: dict[str, str] = {}
    while True:
        line = await _read_http_line(reader)
        header_bytes += len(line)
        if not line or header_bytes > MAX_HTTP_HEADER_BYTES:
            raise OSError("invalid_http_response")
        if line == b"\r\n":
            break
        key, value = _parse_header_line(line)
        if key in headers:
            raise OSError("invalid_http_response")
        headers[key] = value

    transfer_encoding = headers.get("transfer-encoding")
    if transfer_encoding is not None and "content-length" in headers:
        raise OSError("invalid_http_response")
    if method == "HEAD" or status in {204, 304} or 100 <= status < 200:
        raw = b""
    elif transfer_encoding is not None:
        if transfer_encoding.lower() != "chunked":
            raise OSError("invalid_http_response")
        raw = await _read_chunked_body(reader)
    elif "content-length" in headers:
        raw_length = headers["content-length"]
        if not raw_length.isascii() or not raw_length.isdigit():
            raise OSError("invalid_http_response")
        length = int(raw_length)
        if length > MAX_HTTP_RESPONSE_BYTES:
            raise OSError("response_too_large")
        try:
            raw = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise OSError("invalid_http_response") from exc
    else:
        try:
            raw = await reader.readexactly(MAX_HTTP_RESPONSE_BYTES + 1)
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial
        if len(raw) > MAX_HTTP_RESPONSE_BYTES:
            raise OSError("response_too_large")
    return status, raw.decode("utf-8", errors="replace")


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    framing_bytes = 0
    while True:
        size_line = await _read_http_line(reader)
        framing_bytes += len(size_line)
        if framing_bytes > MAX_HTTP_HEADER_BYTES:
            raise OSError("invalid_http_response")
        if not size_line.endswith(b"\r\n") or b";" in size_line:
            raise OSError("invalid_http_response")
        raw_size = size_line[:-2]
        if not raw_size or any(byte not in b"0123456789abcdefABCDEF" for byte in raw_size):
            raise OSError("invalid_http_response")
        try:
            size = int(raw_size, 16)
        except ValueError as exc:
            raise OSError("invalid_http_response") from exc
        if size == 0:
            while True:
                trailer = await _read_http_line(reader)
                framing_bytes += len(trailer)
                if framing_bytes > MAX_HTTP_HEADER_BYTES:
                    raise OSError("invalid_http_response")
                if trailer == b"\r\n":
                    return bytes(body)
                if not trailer:
                    raise OSError("invalid_http_response")
                key, _ = _parse_header_line(trailer)
                if key in {"content-length", "transfer-encoding"}:
                    raise OSError("invalid_http_response")
        if size < 0 or len(body) + size > MAX_HTTP_RESPONSE_BYTES:
            raise OSError("response_too_large")
        try:
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise OSError("invalid_http_response")
        except asyncio.IncompleteReadError as exc:
            raise OSError("invalid_http_response") from exc


async def _read_http_line(reader: asyncio.StreamReader) -> bytes:
    try:
        line = await reader.readline()
    except ValueError as exc:
        raise OSError("invalid_http_response") from exc
    if len(line) > MAX_HTTP_HEADER_BYTES:
        raise OSError("invalid_http_response")
    return line


def _parse_header_line(line: bytes) -> tuple[str, str]:
    if not line.endswith(b"\r\n"):
        raise OSError("invalid_http_response")
    try:
        raw_name, raw_value = line[:-2].split(b":", 1)
    except ValueError as exc:
        raise OSError("invalid_http_response") from exc
    if not raw_name or any(byte not in HTTP_TOKEN_BYTES for byte in raw_name):
        raise OSError("invalid_http_response")
    if any((byte < 32 and byte != 9) or byte == 127 for byte in raw_value):
        raise OSError("invalid_http_response")
    return raw_name.decode("ascii").lower(), raw_value.decode("iso-8859-1").strip()
