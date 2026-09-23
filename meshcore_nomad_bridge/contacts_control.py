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
import os
import re
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

    def _save_favorites(self, favorites: set[str]):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.data_dir / ".nomad-favorites.tmp"
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(sorted(favorites), f)
            os.replace(tmp, self.data_dir / _FAVORITES_FILE)
        finally:
            tmp.unlink(missing_ok=True)

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
        key = request.get("public_key")

        if action in ("favorite", "unfavorite", "remove"):
            if not isinstance(key, str) or re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
                self.result = {"id": req_id, "status": "invalid_key", "action": action}
                publish()
                return
            key = key.lower()

        if action in ("favorite", "unfavorite"):
            assert isinstance(key, str)  # validated above
            updated = set(self._favorites)
            if action == "favorite":
                updated.add(key)
            else:
                updated.discard(key)
            try:
                self._save_favorites(updated)
            except OSError:
                logger.exception("Could not persist contact favorites")
                self.result = {"id": req_id, "status": "error", "action": action}
            else:
                self._favorites = updated
                self.result = {"id": req_id, "status": "ok", "action": action}
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
                assert isinstance(key, str)  # validated before dispatch
                status = await self.meshcore.remove_contact(
                    key,
                    may_send=lambda: time.monotonic() < deadline and self._request() == request,
                )
                self.result = {"id": req_id, "status": status, "action": "remove"}
                if status == "ok" and key in self._favorites:
                    updated = self._favorites - {key}
                    try:
                        self._save_favorites(updated)
                    except OSError:
                        logger.exception("Contact removed but favorite cleanup could not be saved")
                        self.result["favorite_cleanup"] = "error"
                    else:
                        self._favorites = updated
        except asyncio.CancelledError:
            self.result = {"id": req_id, "status": "unknown", "action": action}
            publish()
            raise
        except Exception:
            logger.exception("Contacts action %s outcome unknown", action)
            self.result = {"id": req_id, "status": "unknown", "action": action}
