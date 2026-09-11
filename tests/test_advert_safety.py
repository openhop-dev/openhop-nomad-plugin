import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from meshcore_nomad_bridge import advert_control
from meshcore_nomad_bridge.meshcore_client import MeshCoreClient


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["expiry", "replacement"])
async def test_request_revalidated_after_command_lock(tmp_path, monkeypatch, invalidate):
    mesh = MeshCoreClient(host="127.0.0.1", port=1)
    mesh._connected.set()
    mesh._send_command_expect = AsyncMock(return_value=b"\x00")
    control = advert_control.AdvertControl(tmp_path, mesh)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"advert_request": {"token": control.token, "mode": "flood", "id": "old"}})
    )
    await mesh._command_lock.acquire()
    task = asyncio.create_task(control.tick())
    await asyncio.sleep(0.01)
    if invalidate == "expiry":
        monkeypatch.setattr(advert_control.time, "monotonic", lambda: control.deadline + 31)
    else:
        path.write_text("{}")
    mesh._command_lock.release()
    await task
    mesh._send_command_expect.assert_not_awaited()
    assert control.result["status"] == "expired"


@pytest.mark.asyncio
async def test_send_failure_records_unknown_and_does_not_replay(tmp_path):
    mesh = AsyncMock()
    mesh.send_advert.side_effect = RuntimeError("unexpected failure")
    control = advert_control.AdvertControl(tmp_path, mesh)
    (tmp_path / "config.json").write_text(
        json.dumps({"advert_request": {"token": control.token, "mode": "flood", "id": "fail"}})
    )
    await control.tick()
    await control.tick()
    assert control.result["status"] == "unknown"
    mesh.send_advert.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_socket_failure_discards_connection():
    mesh = MeshCoreClient(host="127.0.0.1", port=1)
    mesh._connected.set()
    mesh._send_command_expect = AsyncMock(side_effect=RuntimeError("protocol failure"))
    assert await mesh.send_advert("flood") == "unknown"
    assert not mesh.connected
    assert not mesh._command_lock.locked()


@pytest.mark.asyncio
async def test_cancelled_socket_command_discards_connection():
    mesh = MeshCoreClient(host="127.0.0.1", port=1)
    mesh._connected.set()
    started = asyncio.Event()

    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    mesh._send_command_expect = wait
    task = asyncio.create_task(mesh.send_advert("flood"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not mesh.connected
    assert not mesh._command_lock.locked()
