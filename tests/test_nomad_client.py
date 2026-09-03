import pytest

from meshcore_nomad_bridge.nomad_client import NomadClient, NomadUnavailable


@pytest.mark.asyncio
async def test_nomad_client_success() -> None:
    async def fake_post(*, url: str, payload: dict[str, object], timeout_seconds: float):
        assert url == "http://nomad.local/api/ollama/chat"
        assert payload["stream"] is False
        assert timeout_seconds == 5
        return 200, '{"message":{"role":"assistant","content":"Hello from NOMAD"},"done":true,"model":"test"}'

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
async def test_nomad_client_persistent_mode_creates_and_reuses_session(tmp_path) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def fake_request(
        *,
        method: str,
        url: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ):
        _ = timeout_seconds
        calls.append((method, url, payload))
        if method == "POST" and url == "http://nomad.local/api/chat/sessions":
            return 201, '{"id":"42","title":"MeshCore sender-a","model":"test-model"}'
        if method == "GET" and url == "http://nomad.local/api/chat/sessions/42":
            return 200, '{"id":"42","messages":[{"role":"user","content":"old q"},{"role":"assistant","content":"old a"}]}'
        if method == "POST" and url == "http://nomad.local/api/ollama/chat":
            assert payload is not None
            assert payload["sessionId"] == 42
            assert payload["messages"] == [
                {"role": "user", "content": "old q"},
                {"role": "assistant", "content": "old a"},
                {"role": "user", "content": "new q"},
            ]
            return 200, '{"message":{"content":"new a"},"done":true,"model":"test"}'
        raise AssertionError(f"Unexpected request: {method} {url}")

    session_map = tmp_path / "sessions.json"
    client = NomadClient(
        base_url="http://nomad.local",
        model="test-model",
        timeout_seconds=5,
        collection=None,
        one_shot=False,
        session_map_path=str(session_map),
        http_request=fake_request,
    )

    result = await client.ask_for_sender("sender-a", "new q")

    assert result == "new a"
    assert session_map.read_text(encoding="utf-8") == '{"sender-a": 42}'
    assert calls[0] == ("POST", "http://nomad.local/api/chat/sessions", {"title": "MeshCore sender-a", "model": "test-model"})


@pytest.mark.asyncio
async def test_nomad_client_reset_session_for_sender_updates_mapping(tmp_path) -> None:
    next_id = 100

    async def fake_request(
        *,
        method: str,
        url: str,
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ):
        nonlocal next_id
        _ = (payload, timeout_seconds)
        if method == "POST" and url == "http://nomad.local/api/chat/sessions":
            next_id += 1
            return 201, f'{{"id":"{next_id}"}}'
        if method == "GET" and url == f"http://nomad.local/api/chat/sessions/{next_id}":
            return 200, '{"id":"101","messages":[]}'
        if method == "POST" and url == "http://nomad.local/api/ollama/chat":
            return 200, '{"message":{"content":"ok"},"done":true,"model":"test"}'
        raise AssertionError(f"Unexpected request: {method} {url}")

    session_map = tmp_path / "sessions.json"
    client = NomadClient(
        base_url="http://nomad.local",
        model="test-model",
        timeout_seconds=5,
        collection=None,
        one_shot=False,
        session_map_path=str(session_map),
        http_request=fake_request,
    )

    first = await client.ask_for_sender("sender-b", "q1")
    assert first == "ok"
    reset_id = await client.reset_session_for_sender("sender-b")
    assert reset_id == 102
    data = session_map.read_text(encoding="utf-8")
    assert data == '{"sender-b": 102}'
