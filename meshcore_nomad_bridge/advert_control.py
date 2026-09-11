"""Bounded one-use advert mailbox via existing authenticated plugin settings.

Only the manager writes config.json. Only this process writes runtime.json.
A random process-local challenge expires after 30 seconds and is consumed before
sending. Saved config, restarts, and repeated POSTs cannot replay an RF action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class AdvertControl:
    def __init__(self, data_dir: Path, meshcore, *, endpoint: str = "") -> None:
        self.data_dir = data_dir
        self.meshcore = meshcore
        self.endpoint = endpoint
        self.result = None
        self._rotate()

    def _rotate(self):
        self.token = secrets.token_hex(32)
        self.deadline = time.monotonic() + 30

    def _publish(self):
        snapshot = {
            "advert": {
                "token": self.token,
                "updated_at": time.time(),
                "connected": bool(self.meshcore.connected),
                "result": self.result,
                "endpoint": self.endpoint,
            }
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.data_dir / ".nomad-runtime.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(snapshot, stream)
        os.replace(temporary, self.data_dir / "runtime.json")

    def _request(self):
        try:
            with (self.data_dir / "config.json").open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return None
            config = json.loads(raw)
        except (OSError, ValueError):
            return None
        return config.get("advert_request") if isinstance(config, dict) else None

    async def tick(self):
        if time.monotonic() >= self.deadline:
            self._rotate()
        request = self._request()
        if (
            isinstance(request, dict)
            and request.get("token") == self.token
            and request.get("mode") in ("zero-hop", "flood")
            and isinstance(request.get("id"), str)
            and 1 <= len(request["id"]) <= 80
        ):
            deadline = self.deadline
            self._rotate()  # consume before any await or transmission
            self.result = {"id": request["id"], "status": "pending", "mode": request["mode"]}
            self._publish()  # failure prevents transmission, not replay
            try:
                self.result["status"] = await self.meshcore.send_advert(
                    request["mode"],
                    may_send=lambda: time.monotonic() < deadline and self._request() == request,
                )
            except asyncio.CancelledError:
                self.result["status"] = "unknown"
                self._publish()
                raise
            except Exception:
                logger.exception("Advert acceptance unknown; request will not be retried")
                self.result["status"] = "unknown"
        self._publish()

    async def run(self, stop_event):
        while not stop_event.is_set():
            try:
                await self.tick()
            except (OSError, ValueError):
                logger.exception("Unable to update advert control mailbox")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
