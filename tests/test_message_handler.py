import asyncio
from dataclasses import replace
from itertools import pairwise

import pytest

from meshcore_nomad_bridge.config import Settings
from meshcore_nomad_bridge.main import (
    NOMAD_BUSY_MESSAGE,
    NOMAD_RESET_MESSAGE,
    NOMAD_TOO_LONG_MESSAGE,
    BridgeService,
    DuplicateCache,
    RequestRateLimiter,
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
        max_pending_requests=4,
        max_requests_per_sender=10,
        max_requests_global=50,
        rate_limit_window_seconds=60.0,
        allowed_sender_prefixes=("010203040506", "111213141516"),
        busy_wait_seconds=0.2,
        max_reply_chunks=4,
        max_chunk_bytes=145,
        reply_chunk_delay_seconds=2.0,
        max_prompt_bytes=1000,
        radio_prompt_enabled=False,
        radio_prompt_template="{question}",
        duplicate_ttl_seconds=600,
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_reply_chunks_are_spaced_except_after_last(monkeypatch) -> None:
    class ChunkedNomad(SlowNomad):
        async def ask_for_sender(self, sender_id: str, prompt: str) -> str:
            _ = (sender_id, prompt)
            return "x" * 95

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("meshcore_nomad_bridge.main.asyncio.sleep", record_sleep)
    settings = replace(_settings(), max_chunk_bytes=40, reply_chunk_delay_seconds=2.0)
    mesh = FakeMeshCore()
    service = BridgeService(settings=settings, meshcore=mesh, nomad=ChunkedNomad())

    await service._handle_message(_msg("chunk this", ts=701))

    assert len(mesh.sent) == 3
    assert delays == pytest.approx([2.0, 2.0], abs=0.01)


def _msg(text: str, ts: int = 1, sender: bytes = b"\x01\x02\x03\x04\x05\x06") -> IncomingMessage:
    return IncomingMessage(
        sender_prefix=sender,
        text=text,
        timestamp=ts,
        txt_type=0,
        path_len=0xFF,
        snr=1.0,
    )


class ExplodingNomad(SlowNomad):
    async def ask_for_sender(self, sender_id: str, prompt: str) -> str:
        raise RuntimeError("unexpected failure")


@pytest.mark.asyncio
async def test_background_task_exception_is_retrieved_and_logged(caplog) -> None:
    service = BridgeService(
        settings=_settings(), meshcore=FakeMeshCore(), nomad=ExplodingNomad(delay=0)
    )

    await service._dispatch_message(_msg("explode", ts=700))
    tasks = list(service._inflight)
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)

    assert "Unhandled message task error" in caplog.text


@pytest.mark.asyncio
async def test_duplicate_message_processed_once() -> None:
    settings = _settings()
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._handle_message(_msg("hello", ts=100))
    await service._handle_message(_msg("hello", ts=100))

    assert nomad.calls == 1


def test_duplicate_key_uses_sha256_digest() -> None:
    service = BridgeService(settings=_settings(), meshcore=FakeMeshCore(), nomad=SlowNomad())

    digest = service._dedupe_key(_msg("hello", ts=100)).rsplit(":", 1)[-1]

    assert len(digest) == 64


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
async def test_zero_busy_wait_uses_immediately_available_capacity() -> None:
    settings = replace(_settings(), max_concurrent_requests=1, busy_wait_seconds=0)
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._handle_message(_msg("first", ts=13))

    assert nomad.calls == 1
    assert all(text != NOMAD_BUSY_MESSAGE for _, text in mesh.sent)


def test_duplicate_cache_expires_entries_below_prune_threshold(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr("meshcore_nomad_bridge.main.time.monotonic", lambda: now)
    cache = DuplicateCache(ttl_seconds=60)

    assert cache.seen("key") is False
    now = 161.0

    assert cache.seen("key") is False


@pytest.mark.asyncio
async def test_dispatch_silently_drops_when_pending_limit_is_full() -> None:
    settings = replace(
        _settings(),
        max_concurrent_requests=1,
        max_pending_requests=1,
        busy_wait_seconds=1.0,
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0.05)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("first", ts=201))
    await service._dispatch_message(_msg("second", ts=202))
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 1
    assert [text for _, text in mesh.sent] == ["A concise answer from NOMAD."]


@pytest.mark.asyncio
async def test_pending_rejection_does_not_mark_message_as_duplicate() -> None:
    settings = replace(
        _settings(),
        max_concurrent_requests=1,
        max_pending_requests=1,
        max_requests_per_sender=3,
        busy_wait_seconds=1.0,
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0.05)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)
    rejected = _msg("retry me", ts=211)

    await service._dispatch_message(_msg("first", ts=210))
    await service._dispatch_message(rejected)
    await asyncio.gather(*list(service._inflight))
    await service._dispatch_message(rejected)
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 2


@pytest.mark.asyncio
async def test_dispatch_silently_rate_limits_each_sender() -> None:
    settings = replace(
        _settings(),
        max_pending_requests=4,
        max_requests_per_sender=1,
        max_requests_global=10,
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("first", ts=301))
    await service._dispatch_message(_msg("second", ts=302))
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 1
    assert [text for _, text in mesh.sent] == ["A concise answer from NOMAD."]


@pytest.mark.asyncio
async def test_dispatch_silently_enforces_global_rate_limit() -> None:
    settings = replace(
        _settings(),
        max_pending_requests=4,
        max_requests_per_sender=3,
        max_requests_global=1,
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("first", ts=401, sender=b"\x01\x02\x03\x04\x05\x06"))
    await service._dispatch_message(_msg("second", ts=402, sender=b"\x11\x12\x13\x14\x15\x16"))
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 1
    assert [text for _, text in mesh.sent] == ["A concise answer from NOMAD."]


@pytest.mark.asyncio
async def test_empty_sender_allowlist_rejects_all_requests() -> None:
    settings = replace(_settings(), allowed_sender_prefixes=())
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("blocked", ts=499))
    await asyncio.sleep(0)

    assert nomad.calls == 0
    assert mesh.sent == []


@pytest.mark.asyncio
async def test_dispatch_silently_rejects_sender_outside_allowlist() -> None:
    settings = replace(
        _settings(),
        allowed_sender_prefixes=("aabbccddeeff",),
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("not allowed", ts=501))
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 0
    assert mesh.sent == []


def test_rate_limiter_allows_requests_after_window_expires(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("meshcore_nomad_bridge.main.time.monotonic", lambda: now[0])
    limiter = RequestRateLimiter(per_sender=1, global_limit=1, window_seconds=60)

    assert limiter.allow("sender") is True
    assert limiter.allow("sender") is False
    now[0] = 161.0
    assert limiter.allow("sender") is True


@pytest.mark.asyncio
async def test_duplicate_retransmission_does_not_consume_rate_capacity() -> None:
    settings = replace(
        _settings(),
        max_pending_requests=4,
        max_requests_per_sender=2,
        max_requests_global=10,
    )
    mesh = FakeMeshCore()
    nomad = SlowNomad(delay=0)
    service = BridgeService(settings=settings, meshcore=mesh, nomad=nomad)

    await service._dispatch_message(_msg("first", ts=601))
    await service._dispatch_message(_msg("first", ts=601))
    await service._dispatch_message(_msg("second", ts=602))
    await asyncio.gather(*list(service._inflight))

    assert nomad.calls == 2


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


@pytest.mark.asyncio
async def test_failed_chunk_stops_remaining_reply(caplog):
    class RejectMesh(FakeMeshCore):
        async def send_text(self, recipient_prefix, text):
            self.sent.append((recipient_prefix, text))
            return False

    mesh = RejectMesh()
    service = BridgeService(
        replace(_settings(), max_chunk_bytes=40, reply_chunk_delay_seconds=0), mesh, SlowNomad(0)
    )
    service._nomad.ask_for_sender = _long_answer
    await service._handle_message(_msg("question"))
    assert len(mesh.sent) == 1
    assert "Stopping reply" in caplog.text


async def _long_answer(*args):
    return "x" * 95


@pytest.mark.asyncio
async def test_empty_allowlist_warns_at_startup(caplog):
    mesh = FakeMeshCore()

    async def run(*args):
        return None

    mesh.run = run
    service = BridgeService(replace(_settings(), allowed_sender_prefixes=()), mesh, SlowNomad(0))
    service._register_signals = lambda: None
    await service.run()
    assert "allowed_sender_prefixes is empty; all senders are denied" in caplog.text


@pytest.mark.asyncio
async def test_outbound_pacing_is_global_including_error_replies():
    times = []

    class TimedMesh(FakeMeshCore):
        async def send_text(self, recipient_prefix, text):
            times.append(asyncio.get_running_loop().time())
            return await super().send_text(recipient_prefix, text)

    service = BridgeService(
        replace(_settings(), reply_chunk_delay_seconds=0.04, max_prompt_bytes=5),
        TimedMesh(),
        SlowNomad(0),
    )
    await asyncio.gather(*(service._handle_message(_msg("too long", ts=i)) for i in range(3)))
    assert len(times) == 3
    assert all(b - a >= 0.035 for a, b in pairwise(times))


@pytest.mark.asyncio
async def test_cancelled_pacing_waiter_does_not_block_next_reply():
    mesh = FakeMeshCore()
    service = BridgeService(
        replace(_settings(), reply_chunk_delay_seconds=0.05, max_prompt_bytes=5), mesh, SlowNomad(0)
    )
    await service._handle_message(_msg("too long", ts=1))
    task = asyncio.create_task(service._handle_message(_msg("too long", ts=2)))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(service._handle_message(_msg("too long", ts=3)), 0.2)
    assert len(mesh.sent) == 2


@pytest.mark.asyncio
async def test_concurrent_chunked_answers_share_pacing():
    times = []

    class TimedMesh(FakeMeshCore):
        async def send_text(self, recipient_prefix, text):
            times.append(asyncio.get_running_loop().time())
            return await super().send_text(recipient_prefix, text)

    mesh = TimedMesh()
    service = BridgeService(
        replace(_settings(), max_chunk_bytes=40, reply_chunk_delay_seconds=0.02), mesh, SlowNomad(0)
    )
    service._nomad.ask_for_sender = _long_answer
    await asyncio.gather(
        service._handle_message(_msg("a", ts=1)), service._handle_message(_msg("b", ts=2))
    )
    assert len(mesh.sent) == 6
    assert all(b - a >= 0.018 for a, b in pairwise(times))


@pytest.mark.asyncio
async def test_cancelling_active_send_releases_pacing_lock():
    entered = asyncio.Event()

    class BlockingMesh(FakeMeshCore):
        async def send_text(self, recipient_prefix, text):
            await super().send_text(recipient_prefix, text)
            if len(self.sent) == 1:
                entered.set()
                await asyncio.Event().wait()
            return True

    mesh = BlockingMesh()
    service = BridgeService(
        replace(_settings(), reply_chunk_delay_seconds=0.02), mesh, SlowNomad(0)
    )
    task = asyncio.create_task(service._handle_message(_msg("first", ts=1)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(service._handle_message(_msg("next", ts=2)), 0.2)
    assert len(mesh.sent) == 2
