"""Contacts address-book control via the existing authenticated plugin mailbox.

Reads contact list requests and remove requests from config.json, executes them
on the running Companion connection, and publishes results to runtime.json.
Favorites are stored in the plugin data directory as a lightweight JSON set of
public-key hex prefixes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_FAVORITES_FILE = "nomad_favorites.json"


class ContactsControl:
    def __init__(self, data_dir: Path, meshcore, endpoint: str) -> None:
        self.data_dir = data_dir
        self.meshcore = meshcore
        self.endpoint = endpoint
        self.result = None
        self._favorites: set[str] = set()
        self._load_favorites()
        self._rotate()

    def _rotate(self):
        self.token = secrets.token_hex(32)
        self.deadline = time.monotonic() + 30

    def _load_favorites(self):
        try:
            path = self.data_dir / _FAVORITES_FILE
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._favorites = {s for s in data if isinstance(s, str)}
        except (OSError, ValueError):
            logger.debug("No existing favorites file or invalid format")

    def _save_favorites(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / ".nomad-favorites.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(sorted(self._favorites), f)
        import os
        os.replace(tmp, self.data_dir / _FAVORITES_FILE)

    def snapshot(self):
        return {
            "token": self.token,
            "updated_at": time.time(),
            "connected": bool(self.meshcore.connected),
            "endpoint": self.endpoint,
            "result": self.result,
            "favorites": sorted(self._favorites),
        }

    def _request(self):
        try:
            with (self.data_dir / "config.json").open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return None
            config = json.loads(raw)
            return config.get("contacts_request") if isinstance(config, dict) else None
        except (OSError, ValueError):
            return None

    async def tick(self, publish):
        if time.monotonic() >= self.deadline:
            self._rotate()

        request = self._request()
        if not (
            isinstance(request, dict)
            and request.get("token") == self.token
            and isinstance(request.get("id"), str)
            and 1 <= len(request["id"]) <= 80
            and request.get("action") in ("list", "remove", "favorite", "unfavorite")
        ):
            return

        deadline = self.deadline
        self._rotate()  # consume before any await
        action = request["action"]
        req_id = request["id"]

        if action == "favorite":
            key = request.get("public_key", "")
            if isinstance(key, str) and len(key) >= 12:
                self._favorites.add(key)
                self._save_favorites()
                self.result = {"id": req_id, "status": "ok", "action": "favorite"}
            else:
                self.result = {"id": req_id, "status": "error", "action": "favorite"}
            publish()
            return

        if action == "unfavorite":
            key = request.get("public_key", "")
            if isinstance(key, str):
                self._favorites.discard(key)
                self._save_favorites()
                self.result = {"id": req_id, "status": "ok", "action": "unfavorite"}
            else:
                self.result = {"id": req_id, "status": "error", "action": "unfavorite"}
            publish()
            return

        self.result = {"id": req_id, "status": "pending", "action": action}
        publish()

        try:
            if action == "list":
                data = await self.meshcore.get_contacts(
                    may_send=lambda: time.monotonic() < deadline and self._request() == request,
                )
                self.result = {
                    "id": req_id,
                    "status": data["status"],
                    "action": "list",
                    "total": data.get("total", 0),
                    "contacts": data.get("contacts", []),
                }
            elif action == "remove":
                pubkey = request.get("public_key", "")
                if not isinstance(pubkey, str) or len(pubkey) < 64:
                    self.result = {"id": req_id, "status": "invalid_key", "action": "remove"}
                else:
                    status = await self.meshcore.remove_contact(
                        pubkey,
                        may_send=lambda: time.monotonic() < deadline and self._request() == request,
                    )
                    self.result = {"id": req_id, "status": status, "action": "remove"}
                    # Also remove from favorites if it was there
                    if status == "ok":
                        self._favorites.discard(pubkey)
                        self._save_favorites()
        except asyncio.CancelledError:
            self.result = {"id": req_id, "status": "unknown", "action": action}
            publish()
            raise
        except Exception:
            logger.exception("Contacts action %s outcome unknown", action)
            self.result = {"id": req_id, "status": "unknown", "action": action}
