"""Async client for Project N.O.M.A.D. chat API."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

MAX_HTTP_RESPONSE_BYTES = 262_144
MAX_HTTP_HEADER_BYTES = 65_536
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
    if method not in {"GET", "POST"}:
        raise OSError("unsupported_http_method")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise OSError("invalid_url")
    # Dedicated c-ares resolver: no asyncio getaddrinfo executor jobs survive
    # cancellation. Passing options avoids aiohttp's shared resolver lifetime.
    resolver = aiohttp.AsyncResolver(tries=1)
    try:
        async with (
            aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False) as connector,
            aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=None),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                max_line_size=8190,
                max_field_size=8190,
                max_headers=128,
                read_bufsize=16_384,
            ) as session,
            session.request(
                method,
                url,
                data=body,
                allow_redirects=False,
                headers={"Content-Type": "application/json"} if body is not None else None,
            ) as response,
        ):
            if any(
                ord(char) < 32 and char != "\t" or ord(char) == 127
                for char in response.reason or ""
            ):
                raise OSError("invalid_http_response")
            if response.headers.get("Transfer-Encoding", "chunked").lower() != "chunked":
                raise OSError("invalid_http_response")
            # Check aggregate size too; parser limits bound each field/count.
            if sum(len(k) + len(v) + 4 for k, v in response.raw_headers) > MAX_HTTP_HEADER_BYTES:
                raise OSError("invalid_http_response")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise OSError("unsupported_content_encoding")
            if (
                response.content_length is not None
                and response.content_length > MAX_HTTP_RESPONSE_BYTES
            ):
                raise OSError("response_too_large")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16_384):
                if len(data) + len(chunk) > MAX_HTTP_RESPONSE_BYTES:
                    raise OSError("response_too_large")
                data.extend(chunk)
            return response.status, data.decode("utf-8", errors="replace")
    except aiohttp.ClientError as exc:
        raise OSError("invalid_http_response") from exc
    finally:
        await resolver.close()
