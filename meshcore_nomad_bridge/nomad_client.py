"""Async client for Project N.O.M.A.D. chat API."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from time import monotonic

import aiohttp

logger = logging.getLogger(__name__)

MAX_HTTP_RESPONSE_BYTES = 262_144
MAX_HTTP_HEADER_BYTES = 65_536
MAX_SESSION_HISTORY_MESSAGES = 20
MAX_SESSION_HISTORY_BYTES = 16_384
MAX_CONVERSATION_SENDERS = 64
CONVERSATION_TTL_SECONDS = 1800


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
        # Retain the argument for compatibility, but never read/write legacy maps.
        _ = session_map_path
        # Fixed lock stripes bound bookkeeping even for an unlimited sender stream.
        # Hash collisions only serialize unrelated senders; histories stay isolated.
        self._sender_locks = [asyncio.Lock() for _ in range(MAX_CONVERSATION_SENDERS)]
        self._histories: OrderedDict[
            str, tuple[float, list[dict[str, str]], asyncio.TimerHandle]
        ] = OrderedDict()

        if http_request is not None:
            self._http_request = http_request
        elif http_post is not None:
            self._http_request = _wrap_post_only(http_post)
        else:
            self._http_request = _default_http_request

    async def close(self) -> None:
        for sender_id in list(self._histories):
            self._forget(sender_id)

    async def ask(self, prompt: str) -> str:
        return await self._chat(messages=[{"role": "user", "content": prompt}])

    async def ask_for_sender(self, sender_id: str, prompt: str) -> str:
        if self._one_shot:
            return await self.ask(prompt)
        async with self._sender_locks[hash(sender_id) % len(self._sender_locks)]:
            self._prune()
            current = {"role": "user", "content": prompt}
            remaining = MAX_SESSION_HISTORY_BYTES - _message_bytes(current)
            if remaining < 0:
                raise NomadUnavailable("prompt_too_large")
            previous = self._histories.get(sender_id)
            history = _limit_pairs(previous[1] if previous else [], remaining)
            answer = await self._chat(messages=[*history, current])
            # Commit only a complete successful pair; failures/cancellation change nothing.
            history = _limit_pairs([*history, current, {"role": "assistant", "content": answer}])
            self._prune()
            self._forget(sender_id)
            if history:
                while len(self._histories) >= MAX_CONVERSATION_SENDERS:
                    self._forget(next(iter(self._histories)))
                timer = asyncio.get_running_loop().call_later(
                    CONVERSATION_TTL_SECONDS, self._forget, sender_id
                )
                self._histories[sender_id] = (monotonic() + CONVERSATION_TTL_SECONDS, history, timer)
            return answer

    async def reset_session_for_sender(self, sender_id: str) -> None:
        async with self._sender_locks[hash(sender_id) % len(self._sender_locks)]:
            self._prune()
            self._forget(sender_id)

    def _forget(self, sender_id: str) -> None:
        entry = self._histories.pop(sender_id, None)
        if entry is not None:
            entry[2].cancel()

    def _prune(self) -> None:
        now = monotonic()
        for sender_id, (expires, _, _) in list(self._histories.items()):
            if expires <= now:
                self._forget(sender_id)

    async def _chat(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> str:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "think": False,
        }
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

def _message_bytes(message: dict[str, str]) -> int:
    return len(message["role"].encode("utf-8")) + len(message["content"].encode("utf-8"))


def _limit_pairs(
    messages: list[dict[str, str]], budget: int = MAX_SESSION_HISTORY_BYTES
) -> list[dict[str, str]]:
    """Keep a contiguous suffix of complete turns, never orphan assistant messages."""
    selected: list[dict[str, str]] = []
    for end in range(len(messages), 1, -2):
        pair = messages[end - 2:end]
        size = sum(_message_bytes(message) for message in pair)
        if len(selected) + 2 > MAX_SESSION_HISTORY_MESSAGES or size > budget:
            break
        selected[0:0] = pair
        budget -= size
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
            # Accept chunked, Content-Length, or Connection: close bodies.
            te = response.headers.get("Transfer-Encoding", "").lower()
            has_content_length = response.content_length is not None
            has_conn_close = "close" in response.headers.get("Connection", "").lower()
            if te == "chunked":
                pass  # chunked body — read iteratively below
            elif has_content_length:
                pass  # fixed-length body — read iteratively below
            elif has_conn_close:
                pass  # body ends at connection close — read iteratively below
            else:
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
