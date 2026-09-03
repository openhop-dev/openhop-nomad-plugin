import asyncio
from dataclasses import replace

import pytest

from meshcore_nomad_bridge.config import Settings
from meshcore_nomad_bridge.main import (
    BridgeService,
    NOMAD_BUSY_MESSAGE,
    NOMAD_RESET_MESSAGE,
    NOMAD_TOO_LONG_MESSAGE,
)
from meshcore_nomad_bridge.meshcore_client import IncomingMessage


class FakeMeshCore:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, str]] = []

    async def send_text(self, recipient_prefix: bytes, text: str) -> bool:
        self.sent.append((recipient_prefix, text))
        return True

    async def close(self) -> None:
        return None


class SlowNomad:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls = 0
        self.current = 0
        self.max_seen = 0
        self.resets = 0

    async def ask_for_sender(self, sender_id: str, prompt: str) -> str:
        _ = (sender_id, prompt)
        self.calls += 1
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        try:
            await asyncio.sleep(self.delay)
            return "A concise answer from NOMAD."
        finally:
            self.current -= 1

    async def reset_session_for_sender(self, sender_id: str) -> int:
        _ = sender_id
        self.resets += 1
        return 1

    async def close(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        meshcore_host="127.0.0.1",
        meshcore_port=5001,
        nomad_url="http://nomad.local",
        nomad_model="qwen3:4b",
        nomad_collection=None,
        nomad_timeout_seconds=120.0,
        one_shot=True,
        nomad_session_map_path="./data/test_nomad_sessions.json",
        max_concurrent_requests=2,
        busy_wait_seconds=0.2,
        max_reply_chunks=4,
        max_chunk_bytes=145,
        max_prompt_bytes=1000,
        radio_prompt_enabled=False,
        radio_prompt_template="{question}",
        duplicate_ttl_seconds=600,
        log_level="INFO",
    )


def _msg(text: str, ts: int = 1) -> IncomingMessage:
    return IncomingMessage(
        sender_prefix=b"\x01\x02\x03\x04\x05\x06",
        text=text,
        timestamp=ts,
        txt_type=0,
        path_len=0xFF,
        snr=1.0,
    )


@pytest.mark.asyncio
async def test_duplicate_message_processed_once() -> None:
    settings = _settings()
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._handle_message(_msg("hello", ts=100))
    await service._handle_message(_msg("hello", ts=100))

    assert nomad.calls == 1


@pytest.mark.asyncio
async def test_prompt_byte_limit_rejected() -> None:
    settings = replace(_settings(), max_prompt_bytes=10)
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._handle_message(_msg("x" * 30, ts=2))

    assert nomad.calls == 0
    assert mesh.sent[-1][1] == NOMAD_TOO_LONG_MESSAGE


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency() -> None:
    settings = replace(_settings(), max_concurrent_requests=2, busy_wait_seconds=1.0)
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0.05)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    tasks = [
        asyncio.create_task(service._handle_message(_msg(f"question {idx}", ts=100 + idx)))
        for idx in range(5)
    ]
    await asyncio.gather(*tasks)

    assert nomad.max_seen <= 2


@pytest.mark.asyncio
async def test_overload_returns_busy_message() -> None:
    settings = replace(_settings(), max_concurrent_requests=1, busy_wait_seconds=0.01)
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0.1)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    t1 = asyncio.create_task(service._handle_message(_msg("first", ts=11)))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(service._handle_message(_msg("second", ts=12)))

    await asyncio.gather(t1, t2)

    assert any(text == NOMAD_BUSY_MESSAGE for _, text in mesh.sent)


@pytest.mark.asyncio
async def test_reset_command_starts_new_session_in_persistent_mode() -> None:
    settings = replace(_settings(), one_shot=False)
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._handle_message(_msg("/reset", ts=77))

    assert nomad.calls == 0
    assert nomad.resets == 1
    assert mesh.sent[-1][1] == NOMAD_RESET_MESSAGE
