"""Tests for the contacts address-book control layer."""

import json

import pytest

from meshcore_nomad_bridge.contacts_control import ContactsControl


class FakeMeshcore:
    """Minimal mock for the MeshCoreClient contacts interface."""

    def __init__(self):
        self.connected = True
        self._contacts = []
        self._remove_result = "ok"

    async def get_contacts(self, *, may_send=None):
        if not self.connected:
            return {"status": "disconnected", "contacts": [], "total": 0}
        if may_send and not may_send():
            return {"status": "expired", "contacts": [], "total": 0}
        return {"status": "ok", "contacts": list(self._contacts), "total": len(self._contacts)}

    async def remove_contact(self, pubkey_hex, *, may_send=None):
        if not self.connected:
            return "disconnected"
        if may_send and not may_send():
            return "expired"
        self.last_removed_key = pubkey_hex
        return self._remove_result


def _write_config(data_dir, contacts_request):
    """Write a config.json with a contacts_request."""
    data_dir.mkdir(parents=True, exist_ok=True)
    config = {"contacts_request": contacts_request}
    (data_dir / "config.json").write_text(json.dumps(config))


@pytest.mark.asyncio
async def test_list_contacts(tmp_path):
    meshcore = FakeMeshcore()
    meshcore._contacts = [
        {"public_key": "aa" * 32, "name": "Alice", "adv_type": 0, "flags": 0,
         "out_path_len": 0, "last_advert": 1000, "lastmod": 2000, "gps_lat": 0, "gps_lon": 0},
        {"public_key": "bb" * 32, "name": "Bob", "adv_type": 1, "flags": 0,
         "out_path_len": 2, "last_advert": 900, "lastmod": 1900, "gps_lat": 0, "gps_lon": 0},
    ]
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    published = []
    _write_config(tmp_path, {"id": "req1", "action": "list", "token": ctrl.token})
    await ctrl.tick(lambda: published.append(True))
    assert ctrl.result is not None
    assert ctrl.result["status"] == "ok"
    assert ctrl.result["action"] == "list"
    assert len(ctrl.result["contacts"]) == 2
    assert ctrl.result["total"] == 2
    assert len(published) >= 1


@pytest.mark.asyncio
async def test_remove_contact(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    pubkey = "cc" * 32
    _write_config(tmp_path, {"id": "req2", "action": "remove", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "ok"
    assert ctrl.result["action"] == "remove"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["aa" * 32 + "ff", "zz" * 32, "aa" * 31, 123])
async def test_invalid_contact_key_never_reaches_radio_or_favorites(tmp_path, bad_key):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    for action in ("remove", "favorite", "unfavorite"):
        _write_config(tmp_path, {"id": action, "action": action, "token": ctrl.token,
                                 "public_key": bad_key})
        await ctrl.tick(lambda: None)
        assert ctrl.result is not None
        assert ctrl.result["status"] == "invalid_key"
        assert not hasattr(meshcore, "last_removed_key")
        assert not ctrl._favorites


@pytest.mark.asyncio
async def test_failed_favorite_save_does_not_change_memory_or_report_success(tmp_path, monkeypatch):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    key = "ee" * 32
    _write_config(tmp_path, {"id": "fav", "action": "favorite", "token": ctrl.token,
                             "public_key": key})
    monkeypatch.setattr(ctrl, "_save_favorites", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    published = []
    await ctrl.tick(lambda: published.append(ctrl.result))
    assert ctrl.result is not None
    assert ctrl.result["status"] == "error"
    assert key not in ctrl._favorites
    assert ctrl.result in published
    assert not (tmp_path / "nomad_favorites.json").exists()


@pytest.mark.asyncio
async def test_failed_unfavorite_save_keeps_prior_favorite(tmp_path, monkeypatch):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    key = "ee" * 32
    _write_config(tmp_path, {"id": "fav", "action": "favorite", "token": ctrl.token,
                             "public_key": key})
    await ctrl.tick(lambda: None)
    monkeypatch.setattr(ctrl, "_save_favorites", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    _write_config(tmp_path, {"id": "unfav", "action": "unfavorite", "token": ctrl.token,
                             "public_key": key})
    await ctrl.tick(lambda: None)
    assert ctrl.result is not None
    assert ctrl.result["status"] == "error"
    assert key in ctrl._favorites
    assert key in json.loads((tmp_path / "nomad_favorites.json").read_text())


@pytest.mark.asyncio
async def test_remove_success_with_failed_favorite_cleanup_reports_partial_result(tmp_path, monkeypatch):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    key = "ee" * 32
    _write_config(tmp_path, {"id": "fav", "action": "favorite", "token": ctrl.token,
                             "public_key": key})
    await ctrl.tick(lambda: None)
    monkeypatch.setattr(ctrl, "_save_favorites", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    _write_config(tmp_path, {"id": "remove", "action": "remove", "token": ctrl.token,
                             "public_key": key})
    await ctrl.tick(lambda: None)
    assert meshcore.last_removed_key == key
    assert ctrl.result == {"id": "remove", "action": "remove", "status": "ok",
                           "favorite_cleanup": "error"}
    assert key in ctrl._favorites


@pytest.mark.asyncio
async def test_remove_contact_not_found(tmp_path):
    meshcore = FakeMeshcore()
    meshcore._remove_result = "not_found"
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    pubkey = "dd" * 32
    _write_config(tmp_path, {"id": "req3", "action": "remove", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "not_found"


@pytest.mark.asyncio
async def test_remove_invalid_key(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    _write_config(tmp_path, {"id": "req4", "action": "remove", "token": ctrl.token, "public_key": "tooshort"})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "invalid_key"


@pytest.mark.asyncio
async def test_favorite_and_unfavorite(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    pubkey = "ee" * 32

    # Favorite
    _write_config(tmp_path, {"id": "fav1", "action": "favorite", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "ok"
    assert ctrl.result["action"] == "favorite"
    assert pubkey in ctrl._favorites
    # Verify persisted
    fav_path = tmp_path / "nomad_favorites.json"
    assert fav_path.exists()
    saved = json.loads(fav_path.read_text())
    assert pubkey in saved

    # Unfavorite
    _write_config(tmp_path, {"id": "unfav1", "action": "unfavorite", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "ok"
    assert ctrl.result["action"] == "unfavorite"
    assert pubkey not in ctrl._favorites


@pytest.mark.asyncio
async def test_favorites_persist_across_instances(tmp_path):
    meshcore = FakeMeshcore()
    pubkey = "ff" * 32

    ctrl1 = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    _write_config(tmp_path, {"id": "fav2", "action": "favorite", "token": ctrl1.token, "public_key": pubkey})
    await ctrl1.tick(lambda: None)
    assert pubkey in ctrl1._favorites

    # New instance should load favorites
    ctrl2 = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    assert pubkey in ctrl2._favorites


@pytest.mark.asyncio
async def test_remove_also_clears_favorite(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    pubkey = "ab" * 32

    # First favorite it
    _write_config(tmp_path, {"id": "fav3", "action": "favorite", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert pubkey in ctrl._favorites

    # Then remove the contact
    _write_config(tmp_path, {"id": "rem1", "action": "remove", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "ok"
    assert pubkey not in ctrl._favorites


@pytest.mark.asyncio
async def test_token_consumed_after_use(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    old_token = ctrl.token
    _write_config(tmp_path, {"id": "req5", "action": "list", "token": old_token})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "ok"
    # Token rotated after consumption
    assert ctrl.token != old_token
    # Same request with old token should be ignored
    ctrl.result = None
    await ctrl.tick(lambda: None)
    assert ctrl.result is None


@pytest.mark.asyncio
async def test_snapshot_includes_favorites(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    pubkey = "cd" * 32
    _write_config(tmp_path, {"id": "fav4", "action": "favorite", "token": ctrl.token, "public_key": pubkey})
    await ctrl.tick(lambda: None)
    snap = ctrl.snapshot()
    assert pubkey in snap["favorites"]
    assert snap["connected"] is True
    assert "token" in snap


@pytest.mark.asyncio
async def test_disconnected_list(tmp_path):
    meshcore = FakeMeshcore()
    meshcore.connected = False
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    _write_config(tmp_path, {"id": "req6", "action": "list", "token": ctrl.token})
    await ctrl.tick(lambda: None)
    assert ctrl.result["status"] == "disconnected"


@pytest.mark.asyncio
async def test_invalid_action_ignored(tmp_path):
    meshcore = FakeMeshcore()
    ctrl = ContactsControl(tmp_path, meshcore, "127.0.0.1:5050")
    _write_config(tmp_path, {"id": "req7", "action": "invalid_action", "token": ctrl.token})
    await ctrl.tick(lambda: None)
    assert ctrl.result is None  # Should be ignored
