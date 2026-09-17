"""Real Companion TCP frames on loopback, never a live radio."""

import asyncio
import contextlib
import struct
from contextlib import asynccontextmanager

import pytest

from meshcore_nomad_bridge.meshcore_client import MeshCoreClient


@asynccontextmanager
async def companion(*, reject=None, truncate=None, mismatch=False):
    frames = []
    state = {"manual": 0xA0, "auto": 0xFE, "mode": 1}
    peers = set()

    async def peer(reader, writer):
        peers.add(asyncio.current_task())
        try:
            while True:
                header = await reader.readexactly(3)
                assert header[0] == 0x3C
                payload = await reader.readexactly(struct.unpack("<H", header[1:])[0])
                frames.append(payload)
                cmd = payload[0]
                if cmd == reject:
                    reply = b"\x01\x01"
                elif cmd == 1:
                    reply = bytearray(58)
                    reply[0] = 5
                    reply[44:48] = bytes([3, 2, 0x39, state["manual"]])
                elif cmd == 22:
                    reply = bytearray(82)
                    reply[0:2] = bytes([13, 10])
                    reply[81] = state["mode"]
                elif cmd == 59:
                    reply = bytes([25, state["auto"], 7])
                else:
                    reply = b"\x00"
                    if not mismatch:
                        if cmd == 38:
                            assert payload[2:] == bytes([0x39, 2, 3])
                            state["manual"] = payload[1]
                        elif cmd == 58:
                            assert len(payload) == 2  # preserve optional max-hop limit
                            state["auto"] = payload[1]
                        elif cmd == 61:
                            assert payload[1] == 0
                            state["mode"] = payload[2]
                        else:
                            raise AssertionError(payload)
                if cmd == truncate:
                    reply = reply[:1]
                writer.write(b">" + struct.pack("<H", len(reply)) + reply)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    client = MeshCoreClient(host="127.0.0.1", port=server.sockets[0].getsockname()[1])
    client._reader, client._writer = await asyncio.open_connection(client._host, client._port)
    client._reader_task = asyncio.create_task(client._reader_loop())
    client._connected.set()
    try:
        yield client, frames, state
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auto,overwrite,size",
    [(a, o, s) for a in ("all", "none") for o in (True, False) for s in (1, 2, 3)],
)
async def test_apply_wire_preserves_other_preferences(auto, overwrite, size):
    async with companion() as (client, frames, state):
        result = await client.companion_settings(
            {"auto_add": auto, "overwrite_oldest": overwrite, "path_hash_bytes": size}
        )
        assert result["status"] == "verified"
        assert result["values"] == {
            "auto_add": auto,
            "overwrite_oldest": overwrite,
            "path_hash_bytes": size,
        }
        assert state["manual"] == (0xA0 if auto == "all" else 0xA1)
        assert state["auto"] == ((0xFE if auto == "all" else 0xE0) | int(overwrite))
        assert state["mode"] == size - 1
        assert all(f[0] in (1, 22, 59, 38, 58, 61) for f in frames)


@pytest.mark.asyncio
async def test_read_and_selective_preservation():
    async with companion() as (client, frames, state):
        state["manual"] = 1
        result = await client.companion_settings({})
        assert result["values"]["auto_add"] == "selected"
        assert [f[0] for f in frames] == [1, 59, 22]
        result = await client.companion_settings({"overwrite_oldest": True})
        assert result["status"] == "verified"
        assert state["manual"] == 1 and state["auto"] == 0xFF
        assert not any(f[0] in (38, 61) for f in frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options,status",
    [
        ({"reject": 59}, "unsupported"),
        ({"truncate": 1}, "unsupported"),
        ({"reject": 58}, "rejected"),
        ({"mismatch": True}, "mismatch"),
    ],
)
async def test_failures_not_claimed_verified(options, status):
    async with companion(**options) as (client, frames, _state):
        result = await client.companion_settings({"overwrite_oldest": True})
        assert result["status"] == status
        assert sum(f[0] == 58 for f in frames) <= 1
        if status == "unsupported":
            assert all(f[0] in (1, 22, 59) for f in frames)


@pytest.mark.asyncio
async def test_partial_and_lock_guard():
    async with companion(reject=61) as (client, _frames, _state):
        result = await client.companion_settings({"overwrite_oldest": True, "path_hash_bytes": 3})
        assert result["status"] == "partial"
    async with companion() as (client, frames, _state):
        await client._command_lock.acquire()
        allowed = True
        task = asyncio.create_task(
            client.companion_settings({"auto_add": "none"}, may_send=lambda: allowed)
        )
        await asyncio.sleep(0)
        allowed = False
        client._command_lock.release()
        assert (await task)["status"] == "expired"
        assert not frames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"auto_add": "selected"},
        {"overwrite_oldest": 1},
        {"path_hash_bytes": True},
        {"path_hash_bytes": 4},
        {"shell": "x"},
    ],
)
async def test_invalid_patch(patch):
    client = MeshCoreClient(host="127.0.0.1", port=1)
    with pytest.raises(ValueError):
        await client.companion_settings(patch)


@pytest.mark.asyncio
async def test_timeout_and_cancellation_close_without_retry(monkeypatch):
    async with companion() as (client, _frames, _state):

        async def fail(*args, **kwargs):
            raise asyncio.TimeoutError

        monkeypatch.setattr(client, "_send_command_expect", fail)
        assert (await client.companion_settings({}))["status"] == "unknown"
        assert not client.connected
    async with companion() as (client, _frames, _state):
        entered = asyncio.Event()

        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_send_command_expect", block)
        task = asyncio.create_task(client.companion_settings({}))
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert not client.connected
        assert not client._command_lock.locked()
