"""Bounded read/modify/read-back of Companion preferences on the running socket.

Wire layouts follow openhop-core 1.1.1 frame_server.py. In particular, short
SET_OTHER_PARAMS frames reset omitted fields there: always send the full tuple.
"""

from __future__ import annotations

import asyncio
import logging

from openhop_core.companion.constants import (
    CMD_APP_START,
    CMD_DEVICE_QUERY,
    CMD_GET_AUTOADD_CONFIG,
    CMD_SET_AUTOADD_CONFIG,
    CMD_SET_OTHER_PARAMS,
    CMD_SET_PATH_HASH_MODE,
    RESP_CODE_AUTOADD_CONFIG,
    RESP_CODE_DEVICE_INFO,
    RESP_CODE_ERR,
    RESP_CODE_OK,
    RESP_CODE_SELF_INFO,
)

logger = logging.getLogger(__name__)
TYPE_BITS = 0x1E


def validate_patch(patch):
    if not isinstance(patch, dict) or set(patch) - {
        "auto_add",
        "overwrite_oldest",
        "path_hash_bytes",
    }:
        raise ValueError("Unknown Companion setting")
    if "auto_add" in patch and patch["auto_add"] not in ("all", "none"):
        raise ValueError("Auto Add must be all or none")
    if "overwrite_oldest" in patch and type(patch["overwrite_oldest"]) is not bool:
        raise ValueError("Overwrite Oldest must be boolean")
    if "path_hash_bytes" in patch and (
        type(patch["path_hash_bytes"]) is not int or patch["path_hash_bytes"] not in (1, 2, 3)
    ):
        raise ValueError("Path hash bytes must be 1, 2 or 3")


class UnsupportedSettings(Exception):
    pass


class ExpiredSettings(Exception):
    pass


def values(raw):
    manual, auto, mode, *_ = raw
    return {
        "auto_add": "all" if not manual & 1 else ("selected" if auto & TYPE_BITS else "none"),
        "overwrite_oldest": bool(auto & 1),
        "path_hash_bytes": mode + 1,
    }


async def operate(client, patch, *, may_send=None):
    validate_patch(patch)
    patch = dict(patch)
    if not client.connected:
        return {"status": "disconnected"}
    try:
        await asyncio.wait_for(client._command_lock.acquire(), timeout=5)
    except asyncio.TimeoutError:
        return {"status": "busy"}
    written = []

    def guard():
        if may_send is not None and not may_send():
            raise ExpiredSettings

    async def command(payload, code):
        guard()  # under lock, immediately before write, also between commands
        return await client._send_command_expect(
            payload, expected_codes={code, RESP_CODE_ERR}, command_label="companion_settings"
        )

    async def read():
        info = await command(bytes([CMD_APP_START]) + b"\x00" * 7, RESP_CODE_SELF_INFO)
        if info[0] != RESP_CODE_SELF_INFO or len(info) < 58:
            raise UnsupportedSettings
        auto = await command(bytes([CMD_GET_AUTOADD_CONFIG]), RESP_CODE_AUTOADD_CONFIG)
        if auto[0] != RESP_CODE_AUTOADD_CONFIG or len(auto) < 2:
            raise UnsupportedSettings
        device = await command(bytes([CMD_DEVICE_QUERY, 3]), RESP_CODE_DEVICE_INFO)
        if device[0] != RESP_CODE_DEVICE_INFO or len(device) < 82 or device[81] > 2:
            raise UnsupportedSettings
        # Include optional max-hop byte in read-back comparison; never write it.
        return (info[47], auto[1], device[81], info[46], info[45], info[44], auto[2:])

    async def transaction():
        before = await read()
        desired = list(before)
        if "auto_add" in patch:
            desired[0] = (before[0] & ~1) | int(patch["auto_add"] == "none")
            if patch["auto_add"] == "none":
                desired[1] &= ~TYPE_BITS
        if "overwrite_oldest" in patch:
            desired[1] = (desired[1] & ~1) | int(patch["overwrite_oldest"])
        if "path_hash_bytes" in patch:
            desired[2] = patch["path_hash_bytes"] - 1
        # Disable selected types before switching manual mode to avoid enabling
        # new auto-add types transiently when moving from All to None.
        commands = [
            (1, bytes([CMD_SET_AUTOADD_CONFIG, desired[1]])),
            (0, bytes([CMD_SET_OTHER_PARAMS, desired[0], *before[3:6]])),
            (2, bytes([CMD_SET_PATH_HASH_MODE, 0, desired[2]])),
        ]
        for index, payload in commands:
            if before[index] == desired[index]:
                continue
            reply = await command(payload, RESP_CODE_OK)
            if reply[0] == RESP_CODE_ERR:
                return {"status": "partial" if written else "rejected", "applied_commands": written}
            if reply != bytes([RESP_CODE_OK]):
                raise RuntimeError("Unexpected Companion settings response")
            written.append(payload[0])
        after = await read() if written else before
        return {
            "status": "verified" if tuple(desired) == after else "mismatch",
            "values": values(after),
        }

    try:
        if not client.connected:
            return {"status": "disconnected"}
        return await asyncio.wait_for(transaction(), timeout=10)
    except ExpiredSettings:
        return {"status": "partial" if written else "expired", "applied_commands": written}
    except UnsupportedSettings:
        return {"status": "partial" if written else "unsupported", "applied_commands": written}
    except (Exception, asyncio.CancelledError) as exc:
        # No transaction IDs. Never reuse a socket after uncertain acceptance.
        client._connected.clear()
        if client._writer is not None:
            client._writer.close()
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.warning("Companion settings outcome unknown; no retry", exc_info=True)
        return {"status": "unknown", "applied_commands": written}
    finally:
        client._command_lock.release()
