"""Real loopback TCP framing, fake Companion only; no radio hardware."""

import asyncio
import struct

import pytest

from meshcore_nomad_bridge.meshcore_client import MeshCoreClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,flag,reply,status",
    [("zero-hop", 0, 0, "accepted"), ("flood", 1, 0, "accepted"), ("flood", 1, 1, "rejected")],
)
async def test_wire(mode, flag, reply, status):
    frames = []

    async def peer(reader, writer):
        try:
            header = await reader.readexactly(3)
            frames.append(header + await reader.readexactly(struct.unpack("<H", header[1:])[0]))
            writer.write(bytes([0x3E, 1, 0, reply]))
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    client = MeshCoreClient(host="127.0.0.1", port=server.sockets[0].getsockname()[1])
    client._reader, client._writer = await asyncio.open_connection(client._host, client._port)
    client._reader_task = asyncio.create_task(client._reader_loop())
    client._connected.set()
    try:
        assert await client.send_advert(mode) == status
        assert frames == [bytes([0x3C, 2, 0, 7, flag])]
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disconnected_never_queues():
    client = MeshCoreClient(host="127.0.0.1", port=1)
    assert await client.send_advert("flood") == "disconnected"


@pytest.mark.asyncio
async def test_timeout_no_retry_and_serialized(monkeypatch):
    client = MeshCoreClient(host="127.0.0.1", port=1)
    client._connected.set()
    calls = []

    async def send(*args, **kwargs):
        calls.append(args[0])
        raise asyncio.TimeoutError

    monkeypatch.setattr(client, "_send_command_expect", send)
    await client._command_lock.acquire()
    task = asyncio.create_task(client.send_advert("flood"))
    await asyncio.sleep(0)
    assert not calls
    client._command_lock.release()
    assert await task == "unknown"
    assert calls == [b"\x07\x01"]
    assert not client._connected.is_set()
    assert await client.send_advert("flood") == "disconnected"


@pytest.mark.asyncio
async def test_invalid_mode():
    client = MeshCoreClient(host="127.0.0.1", port=1)
    with pytest.raises(ValueError):
        await client.send_advert("shell")
