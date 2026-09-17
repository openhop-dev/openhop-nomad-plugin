"""No RF: mailbox tests use only fake clients and temporary files."""

import json
from unittest.mock import AsyncMock

import pytest

from meshcore_nomad_bridge import advert_control


@pytest.mark.asyncio
async def test_one_use_boot_scoped_request(tmp_path):
    mesh = AsyncMock()
    mesh.send_advert.return_value = "accepted"
    control = advert_control.AdvertControl(tmp_path, mesh)
    await control.tick()
    runtime = json.loads((tmp_path / "runtime.json").read_text())["advert"]
    request = {"token": runtime["token"], "mode": "flood", "id": "request-1"}
    (tmp_path / "config.json").write_text(json.dumps({"advert_request": request}))
    await control.tick()
    await control.tick()
    mesh.send_advert.assert_awaited_once()
    assert mesh.send_advert.await_args.args == ("flood",)
    assert (
        json.loads((tmp_path / "runtime.json").read_text())["advert"]["result"]["status"]
        == "accepted"
    )
    restarted = advert_control.AdvertControl(tmp_path, mesh)
    await restarted.tick()
    mesh.send_advert.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shell", "", None, 1])
async def test_invalid_modes_do_not_send(tmp_path, mode):
    mesh = AsyncMock()
    control = advert_control.AdvertControl(tmp_path, mesh)
    await control.tick()
    runtime = json.loads((tmp_path / "runtime.json").read_text())["advert"]
    (tmp_path / "config.json").write_text(
        json.dumps({"advert_request": {"token": runtime["token"], "mode": mode, "id": "bad"}})
    )
    await control.tick()
    mesh.send_advert.assert_not_called()


@pytest.mark.asyncio
async def test_expired_request_never_sends(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(advert_control.time, "monotonic", lambda: now[0])
    mesh = AsyncMock()
    control = advert_control.AdvertControl(tmp_path, mesh)
    await control.tick()
    runtime = json.loads((tmp_path / "runtime.json").read_text())["advert"]
    (tmp_path / "config.json").write_text(
        json.dumps({"advert_request": {"token": runtime["token"], "mode": "flood", "id": "old"}})
    )
    now[0] += 31
    await control.tick()
    mesh.send_advert.assert_not_called()
