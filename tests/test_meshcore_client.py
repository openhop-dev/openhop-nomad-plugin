import asyncio

import pytest
from openhop_core.companion.constants import (
    RESP_CODE_CHANNEL_MSG_RECV_V3,
    RESP_CODE_NO_MORE_MESSAGES,
    RESP_CODE_SENT,
)

from meshcore_nomad_bridge.meshcore_client import MeshCoreClient


@pytest.mark.asyncio
async def test_send_text_retries_after_timeout_and_logs_acceptance(caplog) -> None:
    caplog.set_level("INFO")
    client = MeshCoreClient(host="127.0.0.1", port=5001)
    call_count = 0

    async def fake_send_command_expect(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise asyncio.TimeoutError
        return bytes([RESP_CODE_SENT])

    client._send_command_expect = fake_send_command_expect  # type: ignore[method-assign]

    result = await client.send_text(b"ABCDEF", "hello")

    assert result is True
    assert call_count == 2
    assert "accepted" in caplog.text
    assert "DM ACK" not in caplog.text


@pytest.mark.asyncio
async def test_drain_messages_ignores_channel_frames() -> None:
    client = MeshCoreClient(host="127.0.0.1", port=5001)

    # First sync returns a channel message, second says queue is empty.
    frames = [
        bytes([RESP_CODE_CHANNEL_MSG_RECV_V3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
        bytes([RESP_CODE_NO_MORE_MESSAGES]),
    ]

    async def fake_send_command_expect(*args, **kwargs):  # type: ignore[no-untyped-def]
        _ = (args, kwargs)
        return frames.pop(0)

    seen = []

    async def on_message(msg):  # type: ignore[no-untyped-def]
        seen.append(msg)

    client._send_command_expect = fake_send_command_expect  # type: ignore[method-assign]

    await client._drain_messages(on_message)

    assert seen == []
    assert frames == []
