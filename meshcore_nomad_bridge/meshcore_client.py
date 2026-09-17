"""Async MeshCore Companion TCP client for OpenHop frame protocol."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openhop_core.companion.constants import (
    CMD_APP_START,
    CMD_DEVICE_QUERY,
    CMD_GET_CONTACTS,
    CMD_REMOVE_CONTACT,
    CMD_SEND_SELF_ADVERT,
    CMD_SEND_TXT_MSG,
    CMD_SYNC_NEXT_MESSAGE,
    CONTACT_NAME_SIZE,
    FRAME_INBOUND_PREFIX,
    FRAME_OUTBOUND_PREFIX,
    MAX_PATH_SIZE,
    OUT_PATH_UNKNOWN,
    PUB_KEY_SIZE,
    PUSH_CODE_MSG_WAITING,
    RESP_CODE_CHANNEL_MSG_RECV,
    RESP_CODE_CHANNEL_MSG_RECV_V3,
    RESP_CODE_CONTACT,
    RESP_CODE_CONTACT_MSG_RECV,
    RESP_CODE_CONTACT_MSG_RECV_V3,
    RESP_CODE_CONTACTS_START,
    RESP_CODE_CURR_TIME,
    RESP_CODE_END_OF_CONTACTS,
    RESP_CODE_ERR,
    RESP_CODE_NO_MORE_MESSAGES,
    RESP_CODE_OK,
    RESP_CODE_SENT,
    TXT_TYPE_PLAIN,
)

logger = logging.getLogger(__name__)


_CMD_NAMES = {
    CMD_APP_START: "CMD_APP_START",
    CMD_DEVICE_QUERY: "CMD_DEVICE_QUERY",
    CMD_GET_CONTACTS: "CMD_GET_CONTACTS",
    CMD_REMOVE_CONTACT: "CMD_REMOVE_CONTACT",
    CMD_SEND_TXT_MSG: "CMD_SEND_TXT_MSG",
    CMD_SYNC_NEXT_MESSAGE: "CMD_SYNC_NEXT_MESSAGE",
}

_RESP_NAMES = {
    RESP_CODE_CHANNEL_MSG_RECV: "RESP_CODE_CHANNEL_MSG_RECV",
    RESP_CODE_CHANNEL_MSG_RECV_V3: "RESP_CODE_CHANNEL_MSG_RECV_V3",
    RESP_CODE_CONTACT: "RESP_CODE_CONTACT",
    RESP_CODE_CONTACT_MSG_RECV: "RESP_CODE_CONTACT_MSG_RECV",
    RESP_CODE_CONTACT_MSG_RECV_V3: "RESP_CODE_CONTACT_MSG_RECV_V3",
    RESP_CODE_CONTACTS_START: "RESP_CODE_CONTACTS_START",
    RESP_CODE_CURR_TIME: "RESP_CODE_CURR_TIME",
    RESP_CODE_END_OF_CONTACTS: "RESP_CODE_END_OF_CONTACTS",
    RESP_CODE_ERR: "RESP_CODE_ERR",
    RESP_CODE_NO_MORE_MESSAGES: "RESP_CODE_NO_MORE_MESSAGES",
    RESP_CODE_SENT: "RESP_CODE_SENT",
    PUSH_CODE_MSG_WAITING: "PUSH_CODE_MSG_WAITING",
}


def _cmd_name(code: int) -> str:
    return _CMD_NAMES.get(code, f"CMD_{code}")


def _resp_name(code: int) -> str:
    return _RESP_NAMES.get(code, f"RESP_{code}")


class MeshCoreProtocolError(RuntimeError):
    """Raised when frame protocol invariants are violated."""


@dataclass(frozen=True)
class IncomingMessage:
    sender_prefix: bytes
    text: str
    timestamp: int
    txt_type: int
    path_len: int
    snr: float | None


class MeshCoreClient:
    def __init__(self, *, host: str, port: int) -> None:
        self._host = host
        self._port = port

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        self._connected = asyncio.Event()
        self._stop_requested = asyncio.Event()

        self._command_lock = asyncio.Lock()
        self._response_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._message_waiting = asyncio.Event()

        self._reader_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        self._stop_requested.set()
        self._connected.clear()
        self._message_waiting.set()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def run(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        backoff = [1, 2, 4, 8, 15, 30]
        index = 0

        while not stop_event.is_set() and not self._stop_requested.is_set():
            try:
                await self._connect_and_process(on_message, stop_event)
                index = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                logger.warning("MeshCore connection lost: %s", exc, exc_info=True)

            if stop_event.is_set() or self._stop_requested.is_set():
                break

            delay = backoff[min(index, len(backoff) - 1)]
            index += 1
            logger.info("Reconnecting to MeshCore in %ss...", delay)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def send_advert(self, mode: str, *, may_send: Callable[[], bool] | None = None) -> str:
        """One explicit advert on this connection. Never retry uncertain acceptance."""
        if mode not in ("zero-hop", "flood"):
            raise ValueError("mode must be zero-hop or flood")
        if not self.connected:
            return "disconnected"
        try:
            await asyncio.wait_for(self._command_lock.acquire(), timeout=5)
        except asyncio.TimeoutError:
            return "busy"
        try:
            if not self.connected:
                return "disconnected"
            try:

                async def guarded_send():
                    # Revalidate inside the scheduled coroutine, under the lock,
                    # immediately before the socket write (no intervening await).
                    if may_send is not None and not may_send():
                        return None
                    return await self._send_command_expect(
                        bytes([CMD_SEND_SELF_ADVERT, int(mode == "flood")]),
                        expected_codes={RESP_CODE_OK, RESP_CODE_ERR},
                        command_label="send_advert",
                    )

                frame = await asyncio.wait_for(guarded_send(), timeout=10)
                if frame is None:
                    return "expired"
                if frame[0] == RESP_CODE_OK:
                    return "accepted"
                if frame[0] == RESP_CODE_ERR:
                    return "rejected"
            except asyncio.CancelledError:
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                raise
            except Exception:
                logger.exception("Advert command failed; acceptance unknown")
            # No transaction IDs: discard the uncertain connection so a late OK
            # cannot acknowledge a subsequent action. The run loop reconnects.
            self._connected.clear()
            if self._writer is not None:
                self._writer.close()
            return "unknown"
        finally:
            self._command_lock.release()

    async def companion_settings(self, patch: dict, *, may_send=None) -> dict:
        """Read or explicitly apply preferences using this connection only."""
        from .companion_settings import operate

        return await operate(self, patch, may_send=may_send)

    async def get_contacts(self, *, may_send: Callable[[], bool] | None = None) -> dict:
        """Fetch the full contact list from the Companion.

        Returns a dict with 'status', 'total', and 'contacts' (list of dicts).
        Each contact dict has: public_key (hex), name, adv_type, flags,
        out_path_len, last_advert, lastmod, gps_lat, gps_lon.
        """
        if not self.connected:
            return {"status": "disconnected", "contacts": [], "total": 0}
        try:
            await asyncio.wait_for(self._command_lock.acquire(), timeout=5)
        except asyncio.TimeoutError:
            return {"status": "busy", "contacts": [], "total": 0}
        try:
            if not self.connected:
                return {"status": "disconnected", "contacts": [], "total": 0}
            try:
                if may_send is not None and not may_send():
                    return {"status": "expired", "contacts": [], "total": 0}

                # CMD_GET_CONTACTS takes an optional 4-byte 'since' timestamp
                frame = await asyncio.wait_for(
                    self._send_command_expect(
                        bytes([CMD_GET_CONTACTS]) + struct.pack("<I", 0),
                        expected_codes={RESP_CODE_CONTACTS_START, RESP_CODE_ERR},
                        command_label="get_contacts",
                    ),
                    timeout=15,
                )
                if frame[0] == RESP_CODE_ERR:
                    return {"status": "error", "contacts": [], "total": 0}

                total = struct.unpack("<I", frame[1:5])[0] if len(frame) >= 5 else 0
                contacts = []
                # Collect individual RESP_CODE_CONTACT frames until END_OF_CONTACTS
                while True:
                    resp = await asyncio.wait_for(
                        self._next_command_response(), timeout=10
                    )
                    if resp[0] == RESP_CODE_END_OF_CONTACTS:
                        break
                    if resp[0] == RESP_CODE_CONTACT:
                        parsed = self._parse_contact_frame(resp[1:])
                        if parsed is not None:
                            contacts.append(parsed)
                    # Skip unexpected frames within the contact dump
                return {"status": "ok", "contacts": contacts, "total": total}
            except asyncio.CancelledError:
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                raise
            except Exception:
                logger.exception("get_contacts failed")
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                return {"status": "error", "contacts": [], "total": 0}
        finally:
            self._command_lock.release()

    def _parse_contact_frame(self, data: bytes) -> dict | None:
        """Parse a RESP_CODE_CONTACT body into a dict.

        Layout: pubkey(32) + adv_type(1) + flags(1) + out_path_len(1)
        + out_path(64) + name(32) + last_advert(4) + lat(4i) + lon(4i) + lastmod(4).
        """
        # Minimum: 32 + 3 + 64 + 32 = 131 bytes (without trailing timestamps)
        if len(data) < 131:
            return None
        pubkey = data[:PUB_KEY_SIZE].hex()
        adv_type = data[32]
        flags = data[33]
        opl = data[34]
        out_path_len = -1 if opl == OUT_PATH_UNKNOWN else opl
        # out_path occupies bytes 35..98 (64 bytes), name occupies 99..130 (32 bytes)
        name_start = 35 + MAX_PATH_SIZE
        name_end = name_start + CONTACT_NAME_SIZE
        name = data[name_start:name_end].split(b"\x00")[0].decode("utf-8", errors="replace")
        last_advert = 0
        gps_lat = 0.0
        gps_lon = 0.0
        lastmod = 0
        if len(data) >= name_end + 4:
            last_advert = struct.unpack_from("<I", data, name_end)[0]
        if len(data) >= name_end + 12:
            gps_lat = struct.unpack_from("<i", data, name_end + 4)[0] / 1e6
            gps_lon = struct.unpack_from("<i", data, name_end + 8)[0] / 1e6
        if len(data) >= name_end + 16:
            lastmod = struct.unpack_from("<I", data, name_end + 12)[0]
        return {
            "public_key": pubkey,
            "name": name,
            "adv_type": adv_type,
            "flags": flags,
            "out_path_len": out_path_len,
            "last_advert": last_advert,
            "lastmod": lastmod,
            "gps_lat": gps_lat,
            "gps_lon": gps_lon,
        }

    async def remove_contact(self, pubkey_hex: str, *, may_send: Callable[[], bool] | None = None) -> str:
        """Remove a contact by public key hex. Returns status string."""
        if not self.connected:
            return "disconnected"
        try:
            pubkey = bytes.fromhex(pubkey_hex)
        except ValueError:
            return "invalid_key"
        if len(pubkey) < PUB_KEY_SIZE:
            return "invalid_key"
        try:
            await asyncio.wait_for(self._command_lock.acquire(), timeout=5)
        except asyncio.TimeoutError:
            return "busy"
        try:
            if not self.connected:
                return "disconnected"
            try:
                if may_send is not None and not may_send():
                    return "expired"
                frame = await asyncio.wait_for(
                    self._send_command_expect(
                        bytes([CMD_REMOVE_CONTACT]) + pubkey[:PUB_KEY_SIZE],
                        expected_codes={RESP_CODE_OK, RESP_CODE_ERR},
                        command_label="remove_contact",
                    ),
                    timeout=10,
                )
                if frame[0] == RESP_CODE_OK:
                    return "ok"
                return "not_found"
            except asyncio.CancelledError:
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                raise
            except Exception:
                logger.exception("remove_contact failed; outcome unknown")
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                return "unknown"
        finally:
            self._command_lock.release()

    async def send_text(self, recipient_prefix: bytes, text: str) -> bool:
        if len(recipient_prefix) < 6:
            raise ValueError("recipient_prefix must contain at least 6 bytes")

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                async with self._command_lock:
                    payload = (
                        bytes([CMD_SEND_TXT_MSG, TXT_TYPE_PLAIN, 1])
                        + struct.pack("<I", 0)
                        + recipient_prefix[:6]
                        + text.encode("utf-8", errors="replace")
                    )
                    frame = await self._send_command_expect(
                        payload,
                        expected_codes={RESP_CODE_SENT, RESP_CODE_ERR},
                        command_label="send_text",
                        max_unexpected=4,
                    )

                code = frame[0]
                if code == RESP_CODE_SENT:
                    logger.info(
                        "Companion accepted DM for recipient=%s on attempt %s/%s (not RF delivery confirmation)",
                        recipient_prefix[:6].hex(),
                        attempt,
                        max_retries,
                    )
                    return True
                if code == RESP_CODE_ERR:
                    logger.warning(
                        "MeshCore send_text rejected with error code=%s",
                        frame[1] if len(frame) > 1 else -1,
                    )
                    return False

                logger.warning("Unexpected send_text response code=%s (%s)", code, _resp_name(code))
                return False
            except asyncio.TimeoutError:
                if attempt == max_retries:
                    logger.warning(
                        "No send acceptance received for recipient=%s after %s attempts; outcome unknown",
                        recipient_prefix[:6].hex(),
                        max_retries,
                    )
                    return False

                delay = 1.0 * (2 ** (attempt - 1))
                logger.warning(
                    "No send acceptance received for recipient=%s, retrying in %.1fs (%s/%s)",
                    recipient_prefix[:6].hex(),
                    delay,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(delay)

        return False

    async def _connect_and_process(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        self._reader = reader
        self._writer = writer

        self._response_queue = asyncio.Queue()
        self._message_waiting.clear()

        self._reader_task = asyncio.create_task(self._reader_loop(), name="meshcore-reader")

        await self._startup_handshake()

        self._connected.set()
        logger.info("Connected to MeshCore Companion %s:%s", self._host, self._port)

        try:
            while not stop_event.is_set() and not self._stop_requested.is_set():
                try:
                    await asyncio.wait_for(self._message_waiting.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._message_waiting.set()

                self._message_waiting.clear()
                await self._drain_messages(on_message)

                if self._reader_task.done():
                    exc = self._reader_task.exception()
                    if exc is not None:
                        raise exc
                    raise ConnectionError("MeshCore reader loop exited")
        finally:
            await self._teardown_connection()

    async def _startup_handshake(self) -> None:
        async with self._command_lock:
            await self._send_command_expect(
                bytes([CMD_APP_START]) + b"\x00" * 7,
                expected_codes=None,
                command_label="app_start",
            )

            await self._send_command_expect(
                bytes([CMD_DEVICE_QUERY, 3]),
                expected_codes=None,
                command_label="device_query",
            )

    async def _drain_messages(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        while True:
            async with self._command_lock:
                frame = await self._send_command_expect(
                    bytes([CMD_SYNC_NEXT_MESSAGE]),
                    expected_codes={
                        RESP_CODE_NO_MORE_MESSAGES,
                        RESP_CODE_CHANNEL_MSG_RECV,
                        RESP_CODE_CHANNEL_MSG_RECV_V3,
                        RESP_CODE_CONTACT_MSG_RECV,
                        RESP_CODE_CONTACT_MSG_RECV_V3,
                    },
                    command_label="sync_next_message",
                    max_unexpected=4,
                )

            code = frame[0]
            if code == RESP_CODE_NO_MORE_MESSAGES:
                return

            if code in {RESP_CODE_CONTACT_MSG_RECV, RESP_CODE_CONTACT_MSG_RECV_V3}:
                msg = self._parse_contact_message(frame)
                if msg is not None:
                    await on_message(msg)
                continue

            # Channel messages can be present in the same queue, but this bridge
            # only handles direct contact messages.
            logger.debug("Ignoring non-contact sync response code=%s (%s)", code, _resp_name(code))

    def _parse_contact_message(self, frame: bytes) -> IncomingMessage | None:
        code = frame[0]
        if code == RESP_CODE_CONTACT_MSG_RECV_V3:
            if len(frame) < 16:
                return None
            snr_raw = struct.unpack("b", frame[1:2])[0]
            snr = snr_raw / 4.0
            sender_prefix = frame[4:10]
            path_len = frame[10]
            txt_type = frame[11]
            timestamp = struct.unpack("<I", frame[12:16])[0]
            offset = 16
            text = frame[offset:].decode("utf-8", errors="replace").rstrip("\x00")
            return IncomingMessage(
                sender_prefix=sender_prefix,
                text=text,
                timestamp=timestamp,
                txt_type=txt_type,
                path_len=path_len,
                snr=snr,
            )

        if len(frame) < 13:
            return None
        sender_prefix = frame[1:7]
        path_len = frame[7]
        txt_type = frame[8]
        timestamp = struct.unpack("<I", frame[9:13])[0]
        text = frame[13:].decode("utf-8", errors="replace").rstrip("\x00")
        return IncomingMessage(
            sender_prefix=sender_prefix,
            text=text,
            timestamp=timestamp,
            txt_type=txt_type,
            path_len=path_len,
            snr=None,
        )

    async def _reader_loop(self) -> None:
        reader = self._reader
        if reader is None:
            raise ConnectionError("reader not initialized")

        while True:
            prefix = await reader.readexactly(1)
            if prefix[0] != FRAME_OUTBOUND_PREFIX:
                raise MeshCoreProtocolError(f"Unexpected frame prefix: {prefix[0]:#x}")

            length_bytes = await reader.readexactly(2)
            length = struct.unpack("<H", length_bytes)[0]
            payload = await reader.readexactly(length)

            if not payload:
                continue

            code = payload[0]
            # PUSH frames are unsolicited and must not be queued as command
            # responses. Only MSG_WAITING should trigger a message drain; other
            # pushes are intentionally silent to avoid log spam from routine
            # companion traffic.
            if code >= 0x80:
                if code == PUSH_CODE_MSG_WAITING:
                    logger.debug("IN  push frame code=%s (%s)", code, _resp_name(code))
                    self._message_waiting.set()
                continue

            logger.debug("IN  response frame code=%s (%s)", code, _resp_name(code))
            await self._response_queue.put(payload)

    async def _send_command(self, payload: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("MeshCore socket not connected")
        cmd = payload[0] if payload else -1
        logger.debug("OUT command code=%s (%s) payload_len=%s", cmd, _cmd_name(cmd), len(payload))
        frame = bytes([FRAME_INBOUND_PREFIX]) + struct.pack("<H", len(payload)) + payload
        writer.write(frame)
        await writer.drain()

    async def _next_command_response(self) -> bytes:
        while True:
            frame = await asyncio.wait_for(self._response_queue.get(), timeout=30.0)
            if not frame:
                continue
            if frame[0] == RESP_CODE_CURR_TIME:
                logger.debug("SKIP heartbeat frame code=%s (%s)", frame[0], _resp_name(frame[0]))
                continue
            return frame

    async def _send_command_expect(
        self,
        payload: bytes,
        *,
        expected_codes: set[int] | None,
        command_label: str,
        max_unexpected: int = 2,
    ) -> bytes:
        """Send one command and wait for an expected response frame.

        This protects against occasional stale response frames (e.g. queued
        RESP_CODE_NO_MORE_MESSAGES) being observed while waiting for a different
        command completion.
        """
        await self._send_command(payload)

        if expected_codes is None:
            frame = await self._next_command_response()
            logger.debug(
                "MATCH %s got code=%s (%s)",
                command_label,
                frame[0],
                _resp_name(frame[0]),
            )
            return frame

        unexpected = 0
        while True:
            frame = await self._next_command_response()
            code = frame[0]
            if code in expected_codes:
                logger.debug(
                    "MATCH %s got code=%s (%s)",
                    command_label,
                    code,
                    _resp_name(code),
                )
                return frame

            unexpected += 1
            logger.warning(
                "Unexpected response while waiting for %s: code=%s (%s), expected=%s",
                command_label,
                code,
                _resp_name(code),
                sorted(expected_codes),
            )
            if unexpected >= max_unexpected:
                return frame

    async def _teardown_connection(self) -> None:
        self._connected.clear()

        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
