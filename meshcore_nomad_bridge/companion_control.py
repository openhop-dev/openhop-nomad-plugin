"""One-use Companion settings actions in the manager-owned config mailbox."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from .companion_settings import validate_patch

logger = logging.getLogger(__name__)


class CompanionControl:
    def __init__(self, data_dir, meshcore, endpoint):
        self.data_dir = data_dir
        self.meshcore = meshcore
        self.endpoint = endpoint
        self.result = None
        self._rotate()

    def _rotate(self):
        self.token = secrets.token_hex(32)
        self.deadline = time.monotonic() + 30

    def snapshot(self):
        return {
            "token": self.token,
            "updated_at": time.time(),
            "connected": bool(self.meshcore.connected),
            "endpoint": self.endpoint,
            "result": self.result,
        }

    def _request(self):
        try:
            with (self.data_dir / "config.json").open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return None
            config = json.loads(raw)
            return config.get("companion_request") if isinstance(config, dict) else None
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
        ):
            return
        try:
            validate_patch(request.get("patch"))
        except ValueError:
            return
        deadline = self.deadline
        self._rotate()  # consume before awaiting lock or touching the socket
        self.result = {"id": request["id"], "status": "pending"}
        publish()  # publication failure prevents action
        try:
            result = await self.meshcore.companion_settings(
                request["patch"],
                may_send=lambda: time.monotonic() < deadline and self._request() == request,
            )
            self.result = {**result, "id": request["id"]}
        except asyncio.CancelledError:
            self.result = {"id": request["id"], "status": "unknown"}
            publish()
            raise
        except Exception:
            logger.exception("Companion settings outcome unknown; no retry")
            self.result = {"id": request["id"], "status": "unknown"}
