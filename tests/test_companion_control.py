import asyncio
import json

import pytest
from test_companion_settings import companion

from meshcore_nomad_bridge.advert_control import AdvertControl


@pytest.mark.asyncio
async def test_mailbox_wire_boot_scope_and_preserved_config(tmp_path):
    async with companion() as (client, frames, _state):
        control = AdvertControl(tmp_path, client, endpoint="fake:5050")
        await control.tick()
        runtime = json.loads((tmp_path / "runtime.json").read_text())
        assert "companion" in runtime
        assert not frames  # startup doesn't touch device preferences
        request = {
            "id": "apply",
            "token": runtime["companion"]["token"],
            "patch": {"auto_add": "none"},
        }
        config = {"future": 42, "companion_request": request}
        text = json.dumps(config)
        (tmp_path / "config.json").write_text(text)
        await control.tick()
        result = json.loads((tmp_path / "runtime.json").read_text())["companion"]["result"]
        assert result["id"] == "apply" and result["status"] == "verified"
        assert result["values"]["auto_add"] == "none"
        assert (tmp_path / "config.json").read_text() == text
        count = len(frames)
        await control.tick()
        await AdvertControl(tmp_path, client).tick()
        assert len(frames) == count


@pytest.mark.asyncio
async def test_mailbox_pending_consumption_and_replacement(tmp_path):
    async with companion() as (client, frames, _state):
        control = AdvertControl(tmp_path, client)
        await control.tick()
        runtime = json.loads((tmp_path / "runtime.json").read_text())["companion"]
        request = {"id": "read", "token": runtime["token"], "patch": {}}
        (tmp_path / "config.json").write_text(json.dumps({"companion_request": request}))
        await client._command_lock.acquire()
        task = asyncio.create_task(control.tick())
        await asyncio.sleep(0.01)
        pending = json.loads((tmp_path / "runtime.json").read_text())["companion"]
        assert pending["result"]["status"] == "pending"
        assert pending["token"] != request["token"]
        (tmp_path / "config.json").write_text("{}")
        client._command_lock.release()
        await task
        assert not frames
        assert (
            json.loads((tmp_path / "runtime.json").read_text())["companion"]["result"]["status"]
            == "expired"
        )


@pytest.mark.asyncio
async def test_mailbox_expiry_and_invalid(tmp_path, monkeypatch):
    from meshcore_nomad_bridge import companion_control

    now = [100.0]
    monkeypatch.setattr(companion_control.time, "monotonic", lambda: now[0])
    async with companion() as (client, frames, _state):
        control = AdvertControl(tmp_path, client)
        await control.tick()
        runtime = json.loads((tmp_path / "runtime.json").read_text())["companion"]
        request = {"id": "old", "token": runtime["token"], "patch": {"auto_add": "none"}}
        (tmp_path / "config.json").write_text(json.dumps({"companion_request": request}))
        now[0] += 31
        await control.tick()
        request["token"] = json.loads((tmp_path / "runtime.json").read_text())["companion"]["token"]
        request["patch"] = {"path_hash_bytes": True}
        (tmp_path / "config.json").write_text(json.dumps({"companion_request": request}))
        await control.tick()
        assert not frames
