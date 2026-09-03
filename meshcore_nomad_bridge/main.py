"""Service entrypoint for meshcore-nomad-bridge."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
import time
from typing import Any

from .config import ConfigError, Settings
from .meshcore_client import IncomingMessage, MeshCoreClient
from .nomad_client import NomadClient, NomadUnavailable
from .text import clean_for_radio, split_for_meshcore


logger = logging.getLogger(__name__)

NOMAD_UNAVAILABLE_MESSAGE = "NOMAD is temporarily unavailable. Please try again."
NOMAD_BUSY_MESSAGE = "NOMAD is busy. Please try again shortly."
NOMAD_TOO_LONG_MESSAGE = "Message is too long for NOMAD."
NOMAD_RESET_MESSAGE = "Started a new NOMAD session for this sender."


class DuplicateCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._data: dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        if key in self._data:
            return True
        self._data[key] = now + self._ttl_seconds
        return False

    def _prune(self, now: float) -> None:
        if len(self._data) < 64:
            return
        self._data = {k: exp for k, exp in self._data.items() if exp > now}


class BridgeService:
    def __init__(self, settings: Settings, meshcore: MeshCoreClient, nomad: NomadClient) -> None:
        self._settings = settings
        self._meshcore = meshcore
        self._nomad = nomad

        self._stop_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._duplicate_cache = DuplicateCache(settings.duplicate_ttl_seconds)

        self._inflight: set[asyncio.Task[Any]] = set()

    async def run(self) -> None:
        self._register_signals()
        await self._meshcore.run(self._dispatch_message, self._stop_event)
        await self._shutdown()

    async def _dispatch_message(self, message: IncomingMessage) -> None:
        task = asyncio.create_task(self._handle_message(message), name="handle-message")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _handle_message(self, message: IncomingMessage) -> None:
        if message.txt_type != 0:
            return

        prompt = message.text.strip()
        if not prompt:
            return

        sender_id = message.sender_prefix.hex()

        dedupe_key = self._dedupe_key(message)
        if self._duplicate_cache.seen(dedupe_key):
            logger.debug("Skipping duplicate message from %s", sender_id)
            return

        if (not self._settings.one_shot) and prompt.lower() in {"/new", "/reset"}:
            try:
                await self._nomad.reset_session_for_sender(sender_id)
            except NomadUnavailable:
                await self._meshcore.send_text(message.sender_prefix, NOMAD_UNAVAILABLE_MESSAGE)
            else:
                await self._meshcore.send_text(message.sender_prefix, NOMAD_RESET_MESSAGE)
            return

        if len(prompt.encode("utf-8")) > self._settings.max_prompt_bytes:
            await self._meshcore.send_text(message.sender_prefix, NOMAD_TOO_LONG_MESSAGE)
            return

        acquired = False
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._settings.busy_wait_seconds)
            acquired = True
        except asyncio.TimeoutError:
            await self._meshcore.send_text(message.sender_prefix, NOMAD_BUSY_MESSAGE)
            return

        try:
            logger.info("Message received from %s", message.sender_prefix.hex())
            logger.info("Sending request to NOMAD")
            answer = await self._nomad.ask_for_sender(sender_id, self._build_nomad_prompt(prompt))
        except NomadUnavailable:
            await self._meshcore.send_text(message.sender_prefix, NOMAD_UNAVAILABLE_MESSAGE)
            return
        finally:
            if acquired:
                self._semaphore.release()

        cleaned = clean_for_radio(answer)
        if not cleaned:
            cleaned = NOMAD_UNAVAILABLE_MESSAGE

        chunks = split_for_meshcore(
            cleaned,
            max_bytes=self._settings.max_chunk_bytes,
            max_chunks=self._settings.max_reply_chunks,
        )
        if not chunks:
            chunks = [NOMAD_UNAVAILABLE_MESSAGE]

        logger.info("Sending %d MeshCore reply packets", len(chunks))
        for chunk in chunks:
            await self._meshcore.send_text(message.sender_prefix, chunk)

    def _build_nomad_prompt(self, prompt: str) -> str:
        if not self._settings.radio_prompt_enabled:
            return prompt
        return self._settings.radio_prompt_template.format(question=prompt)

    def _dedupe_key(self, message: IncomingMessage) -> str:
        digest = hashlib.sha1(message.text.strip().encode("utf-8")).hexdigest()
        return f"{message.sender_prefix.hex()}:{message.timestamp}:{digest}"

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            self._stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                signal.signal(sig, lambda *_: _signal_handler())

    async def _shutdown(self) -> None:
        self._stop_event.set()

        pending = list(self._inflight)
        if pending:
            done, not_done = await asyncio.wait(pending, timeout=10)
            for task in not_done:
                task.cancel()
            if not_done:
                await asyncio.gather(*not_done, return_exceptions=True)
            _ = done

        await self._nomad.close()
        await self._meshcore.close()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _async_main() -> int:
    settings = Settings.from_env()
    _configure_logging(settings.log_level)

    meshcore = MeshCoreClient(host=settings.meshcore_host, port=settings.meshcore_port)
    nomad = NomadClient(
        base_url=settings.nomad_url,
        model=settings.nomad_model,
        timeout_seconds=settings.nomad_timeout_seconds,
        collection=settings.nomad_collection,
        one_shot=settings.one_shot,
        session_map_path=settings.nomad_session_map_path,
    )

    service = BridgeService(settings=settings, meshcore=meshcore, nomad=nomad)
    await service.run()
    return 0


def main() -> int:
    try:
        return asyncio.run(_async_main())
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
