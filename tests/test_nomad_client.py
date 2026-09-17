import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meshcore_nomad_bridge import nomad_client
from meshcore_nomad_bridge.nomad_client import NomadClient, NomadUnavailable


async def _request_raw_response(raw_response: bytes) -> tuple[int, str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(raw_response)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        return await nomad_client._default_http_request(
            method="GET",
            url=f"http://127.0.0.1:{port}/test",
            payload=None,
            timeout_seconds=2,
        )


class RedirectHandler(BaseHTTPRequestHandler):
    followed = False

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/followed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).followed = True
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_http_request_rejects_control_characters_in_target() -> None:
    with pytest.raises(OSError, match="invalid_url"):
        await nomad_client._default_http_request(
            method="GET",
            url="http://127.0.0.1/ok\r\nX-Injected: yes",
            payload=None,
            timeout_seconds=2,
        )


@pytest.mark.asyncio
async def test_default_http_request_does_not_follow_redirects() -> None:
    RedirectHandler.followed = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = await nomad_client._default_http_request(
            method="GET",
            url=f"http://127.0.0.1:{server.server_port}/redirect",
            payload=None,
            timeout_seconds=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert status == 302
    assert body == ""
    assert RedirectHandler.followed is False


class SlowTrickleHandler(BaseHTTPRequestHandler):
    disconnected = threading.Event()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "20")
        self.end_headers()
        for _ in range(20):
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                type(self).disconnected.set()
                break
            time.sleep(0.05)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_default_http_request_enforces_total_deadline(monkeypatch) -> None:
    async def forbid_worker_thread(*args: object, **kwargs: object):
        raise AssertionError("HTTP transport must not use a non-cancellable worker thread")

    monkeypatch.setattr(asyncio, "to_thread", forbid_worker_thread)
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowTrickleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises((OSError, TimeoutError)):
            await nomad_client._default_http_request(
                method="GET",
                url=f"http://127.0.0.1:{server.server_port}/slow",
                payload=None,
                timeout_seconds=0.15,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert time.monotonic() - started < 0.7


@pytest.mark.asyncio
async def test_connection_close_body_waits_for_all_fragments() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"def")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        status, body = await nomad_client._default_http_request(
            method="GET",
            url=f"http://127.0.0.1:{port}/fragmented",
            payload=None,
            timeout_seconds=2,
        )

    assert status == 200
    assert body == "abcdef"


@pytest.mark.asyncio
async def test_oversized_http_header_is_a_controlled_network_error() -> None:
    raw = b"HTTP/1.1 200 OK\r\nX-Large: " + (b"x" * 70_000) + b"\r\n\r\n"

    with pytest.raises(OSError, match="invalid_http_response"):
        await _request_raw_response(raw)


@pytest.mark.asyncio
async def test_chunked_response_is_decoded() -> None:
    status, body = await _request_raw_response(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n"
    )

    assert status == 200
    assert body == "abcdef"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_response",
    [
        b"HTTP/1.1 20 Weird\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 bad\x00reason\r\nContent-Length: 2\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\nContent-Length: +2\r\n\r\n{}",
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n"
            b"2\r\n{}\r\n0\r\n\r\n"
        ),
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: notchunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\ngarbage\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;bad=\x00\r\n{}\r\n0\r\n\r\n"),
    ],
)
async def test_malformed_http_framing_is_rejected(raw_response: bytes) -> None:
    with pytest.raises(OSError, match="invalid_http_response"):
        await _request_raw_response(raw_response)


@pytest.mark.asyncio
async def test_chunked_response_rejects_missing_terminal_line() -> None:
    with pytest.raises(OSError, match="invalid_http_response"):
        await _request_raw_response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\n"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 262_144])
async def test_response_body_accepts_exact_size_boundary(size: int) -> None:
    status, body = await _request_raw_response(
        f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode() + b"x" * size
    )

    assert status == 200
    assert len(body) == size


@pytest.mark.asyncio
async def test_default_http_request_converts_malformed_http_to_oserror() -> None:
    with pytest.raises(OSError, match="invalid_http_response"):
        await _request_raw_response(b"broken\r\n\r\n")


@pytest.mark.asyncio
async def test_default_http_request_rejects_oversized_success_body() -> None:
    with pytest.raises(OSError, match="response_too_large"):
        await _request_raw_response(b"HTTP/1.1 200 OK\r\nContent-Length: 262145\r\n\r\n")


@pytest.mark.asyncio
async def test_default_http_request_rejects_oversized_error_body() -> None:
    with pytest.raises(OSError, match="response_too_large"):
        await _request_raw_response(b"HTTP/1.1 500 Error\r\nContent-Length: 262145\r\n\r\n")


@pytest.mark.asyncio
async def test_nomad_client_success() -> None:
    async def fake_post(*, url: str, payload: dict[str, object], timeout_seconds: float):
        assert url == "http://nomad.local/api/ollama/chat"
        assert payload["stream"] is False
        assert timeout_seconds == 5
        return (
            200,
            '{"message":{"role":"assistant","content":"Hello from NOMAD"},"done":true,"model":"test"}',
        )

    client = NomadClient(
        base_url="http://nomad.local",
        model="test-model",
        timeout_seconds=5,
        collection=None,
        http_post=fake_post,
    )
    result = await client.ask("ping")

    assert result == "Hello from NOMAD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,body",
    [
        (500, '{"error":"boom"}'),
        (302, '{"message":{"content":"redirect body"}}'),
        (200, "not json"),
        (200, '{"message":{}}'),
        (200, '{"message":{"content":""}}'),
        (200, "{}"),
    ],
)
async def test_nomad_client_invalid_or_error_responses_raise(status_code: int, body: str) -> None:
    async def fake_post(*, url: str, payload: dict[str, object], timeout_seconds: float):
        _ = (url, payload, timeout_seconds)
        return status_code, body

    client = NomadClient(
        base_url="http://nomad.local",
        model="test-model",
        timeout_seconds=5,
        collection="Emergency Manuals",
        http_post=fake_post,
    )
    with pytest.raises(NomadUnavailable):
        await client.ask("ping")


@pytest.mark.asyncio
async def test_nomad_client_timeout_raises_unavailable() -> None:
    async def fake_post(*, url: str, payload: dict[str, object], timeout_seconds: float):
        _ = (url, payload, timeout_seconds)
        raise TimeoutError("timeout")

    client = NomadClient(
        base_url="http://nomad.local",
        model="test-model",
        timeout_seconds=0.001,
        collection=None,
        http_post=fake_post,
    )
    with pytest.raises(NomadUnavailable):
        await client.ask("ping")


@pytest.mark.asyncio
async def test_hostname_http_request_with_repeated_standard_headers():
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\nContent-Length: 2\r\n\r\n{}"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        result = await nomad_client._default_http_request(
            method="GET",
            url=f"http://localhost:{server.sockets[0].getsockname()[1]}/",
            payload=None,
            timeout_seconds=2,
        )
    assert result == (200, "{}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"HTTP/1.1 200 OK\r\n" + b"X: a\r\n" * 129 + b"\r\n",
        b"HTTP/1.1 200 OK\r\n" + b"X: " + b"a" * 8191 + b"\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n" + (b"X: " + b"a" * 8000 + b"\r\n") * 9 + b"\r\n",
    ],
)
async def test_header_limits(raw):
    with pytest.raises(OSError, match="invalid_http_response"):
        await _request_raw_response(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", [b"Connection: close\r\n", b"Transfer-Encoding: chunked\r\n"])
async def test_streaming_body_limit_without_content_length(framing):
    body = b"x" * 262145
    if b"chunked" in framing:
        body = b"40001\r\n" + body + b"\r\n0\r\n\r\n"
    with pytest.raises(OSError, match="response_too_large"):
        await _request_raw_response(b"HTTP/1.1 200 OK\r\n" + framing + b"\r\n" + body)


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_without_decompression():
    with pytest.raises(OSError, match="unsupported_content_encoding"):
        await _request_raw_response(
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 0\r\n\r\n"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_inflight_http_timeout_or_cancellation_closes_socket(cancel):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await reader.read()
            closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        task = asyncio.create_task(
            nomad_client._default_http_request(
                method="GET",
                url=f"http://localhost:{server.sockets[0].getsockname()[1]}/",
                payload=None,
                timeout_seconds=0.15 if not cancel else 5,
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("respond", [True, False])
async def test_cares_dns_resolution_and_deadline_without_executor(monkeypatch, respond):
    import functools
    import struct

    queried = asyncio.Event()

    class DNS(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, address):
            queried.set()
            if not respond:
                return
            end = 12
            while data[end]:
                end += data[end] + 1
            end += 1
            qtype = struct.unpack("!H", data[end : end + 2])[0]
            question = data[12 : end + 4]
            answer = (
                (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 10, 4) + b"\x7f\x00\x00\x01")
                if qtype == 1
                else b""
            )
            self.transport.sendto(
                data[:2] + struct.pack("!HHHHH", 0x8180, 1, bool(answer), 0, 0) + question + answer,
                address,
            )

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(DNS, local_addr=("127.0.0.1", 0))
    real_resolver = nomad_client.aiohttp.AsyncResolver
    monkeypatch.setattr(
        nomad_client.aiohttp,
        "AsyncResolver",
        functools.partial(
            real_resolver,
            nameservers=["127.0.0.1"],
            udp_port=transport.get_extra_info("sockname")[1],
        ),
    )

    def forbid_executor(*args, **kwargs):
        raise AssertionError("DNS must not use blocking getaddrinfo workers")

    monkeypatch.setattr(loop, "run_in_executor", forbid_executor)

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    before = asyncio.all_tasks()
    try:
        async with server:
            request = nomad_client._default_http_request(
                method="GET",
                url=f"http://nomad_admin.test:{server.sockets[0].getsockname()[1]}/",
                payload=None,
                timeout_seconds=1 if respond else 0.1,
            )
            if respond:
                assert await request == (200, "{}")
            else:
                with pytest.raises(TimeoutError):
                    await request
        assert queried.is_set()
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - before)
    finally:
        transport.close()
