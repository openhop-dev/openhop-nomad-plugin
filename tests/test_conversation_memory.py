"""Bounded, plugin-owned conversation contract (no inference or RF services)."""
import asyncio
import json

import pytest

from meshcore_nomad_bridge import nomad_client as module
from meshcore_nomad_bridge.nomad_client import NomadClient, NomadUnavailable


def client_for(calls, **kwargs):
    async def request(**request):
        assert request['method'] == 'POST'
        assert request['url'].endswith('/api/ollama/chat')
        assert 'sessionId' not in request['payload']
        calls.append(request['payload']['messages'])
        return 200, json.dumps({'message': {'content': 'answer'}})
    return NomadClient(base_url='http://nomad.local', model='test', timeout_seconds=1,
                       collection=None, one_shot=False, http_request=request, **kwargs)


@pytest.mark.asyncio
async def test_memory_is_local_isolated_resettable_and_legacy_file_untouched(tmp_path):
    calls = []
    legacy = tmp_path / 'sessions.json'
    legacy.write_text('{"a":42}')
    client = client_for(calls, session_map_path=str(legacy))
    await client.ask_for_sender('a', 'first')
    await client.ask_for_sender('b', 'other')
    await client.ask_for_sender('a', 'second')
    assert [m['content'] for m in calls[-1]] == ['first', 'answer', 'second']
    assert len(calls[1]) == 1
    await client.reset_session_for_sender('a')
    await client.ask_for_sender('a', 'fresh')
    assert len(calls[-1]) == 1
    await client.ask_for_sender('b', 'still here')
    assert calls[-1][0]['content'] == 'other'
    assert legacy.read_text() == '{"a":42}'
    await client.close()


@pytest.mark.asyncio
async def test_history_count_bytes_capacity_and_expiry(monkeypatch):
    calls = []
    client = client_for(calls)
    for i in range(15):
        await client.ask_for_sender('a', str(i))
    assert len(calls[-1]) <= 21
    assert calls[-1][0]['content'] == '4'
    for i in range(5):
        await client.ask_for_sender('a', 'é' * 3000)
    assert sum(len(m['content'].encode()) + len(m['role']) for m in calls[-1]) <= 16384
    assert calls[-1][-1]['content'] == 'é' * 3000
    for i in range(65):
        await client.ask_for_sender(str(i), 'q')
    assert len(client._histories) == 64
    await client.ask_for_sender('a', 'evicted')
    assert len(calls[-1]) == 1
    now = module.monotonic()
    monkeypatch.setattr(module, 'monotonic', lambda: now + 1801)
    await client.ask_for_sender('a', 'expired')
    assert len(calls[-1]) == 1
    assert len(client._histories) == 1
    await client.close()
    assert not client._histories


@pytest.mark.asyncio
async def test_oversized_latest_prompt_rejected_not_silently_dropped():
    calls = []
    client = client_for(calls)
    with pytest.raises(NomadUnavailable, match='prompt_too_large'):
        await client.ask_for_sender('a', 'é' * 8193)
    assert not calls
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_or_cancelled_turn_does_not_poison_history(cancel):
    calls = []
    client = client_for(calls)
    await client.ask_for_sender('a', 'good')
    original = client._http_request
    entered = asyncio.Event()
    async def failure(**kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise OSError('unavailable')
    client._http_request = failure
    task = asyncio.create_task(client.ask_for_sender('a', 'bad'))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else NomadUnavailable):
        await task
    client._http_request = original
    await client.ask_for_sender('a', 'next')
    assert [m['content'] for m in calls[-1]] == ['good', 'answer', 'next']
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_asks_and_reset_are_serialized():
    calls = []
    client = client_for(calls)
    original = client._http_request
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)
    client._http_request = blocked
    first = asyncio.create_task(client.ask_for_sender('a', 'first'))
    await entered.wait()
    second = asyncio.create_task(client.ask_for_sender('a', 'second'))
    await asyncio.sleep(0)
    reset = asyncio.create_task(client.reset_session_for_sender('a'))
    await asyncio.sleep(0)
    assert not reset.done()
    release.set()
    await asyncio.gather(first, second, reset)
    assert calls[1][0]['content'] == 'first'
    await client.ask_for_sender('a', 'fresh')
    assert len(calls[-1]) == 1
    assert len(client._sender_locks) == 64
    await client.close()


@pytest.mark.asyncio
async def test_idle_expiry_without_new_requests(monkeypatch):
    monkeypatch.setattr(module, 'CONVERSATION_TTL_SECONDS', 0.01)
    client = client_for([])
    await client.ask_for_sender('a', 'q')
    await asyncio.sleep(0.03)
    assert not client._histories
    await client.close()


@pytest.mark.asyncio
async def test_large_answer_drops_complete_pair_and_one_shot_keeps_nothing():
    calls = []
    client = client_for(calls)
    original = client._http_request
    async def huge(**kwargs):
        return 200, json.dumps({'message': {'content': 'x' * 17000}})
    client._http_request = huge
    await client.ask_for_sender('a', 'q')
    assert not client._histories
    client._http_request = original
    client._one_shot = True
    await client.ask_for_sender('a', 'first')
    await client.ask_for_sender('a', 'second')
    assert all(len(messages) == 1 for messages in calls)
    assert not client._histories
    await client.close()


@pytest.mark.asyncio
async def test_real_http_transport_sends_history_without_remote_session_routes():
    calls = []
    async def handle(reader, writer):
        headers = await reader.readuntil(b'\r\n\r\n')
        assert headers.startswith(b'POST /api/ollama/chat HTTP/1.1')
        length = next(int(line.split(b':')[1]) for line in headers.split(b'\r\n')
                      if line.lower().startswith(b'content-length:'))
        calls.append(json.loads(await reader.readexactly(length)))
        body = b'{"message":{"role":"assistant","content":"ok"}}'
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                     + b'\r\nConnection: close\r\n\r\n' + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    async with server:
        client = NomadClient(base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',
                             model='test', collection=None, timeout_seconds=2, one_shot=False)
        try:
            await client.ask_for_sender('a', 'first')
            await client.ask_for_sender('a', 'second')
            await client.reset_session_for_sender('a')
            assert len(calls) == 2
            assert all('sessionId' not in payload for payload in calls)
            assert [m['content'] for m in calls[1]['messages']] == ['first', 'ok', 'second']
        finally:
            await client.close()
