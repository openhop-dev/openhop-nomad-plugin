# openHop NOMAD plugin

A lightweight openHop service plugin that bridges a dedicated Repeater **Companion frame server** identity to Project N.O.M.A.D.

The plugin intentionally keeps the existing MeshCore Companion protocol client. It does not import Repeater internals and does not require a second REST/SSE bridge layer.

## Runtime flow

```text
MeshCore user
    -> direct message
openHop Repeater Companion identity
    -> Companion frame server TCP
NOMAD Bridge plugin
    -> Project N.O.M.A.D. HTTP API
NOMAD Bridge plugin
    -> Companion frame server TCP reply
MeshCore user
```

The expected default Companion endpoint is `127.0.0.1:5001`. Give NOMAD a dedicated Repeater Companion identity and bind its frame server to localhost when the plugin runs on the same host.

Example Repeater configuration:

```yaml
identities:
  companions:
    - name: "NomadBot"
      identity_key: "PRIVATE_IDENTITY_KEY"
      settings:
        node_name: "NOMAD"
        tcp_port: 5001
        bind_address: "127.0.0.1"
        tcp_timeout: 0
```

## Plugin data

The plugin manager should provide a persistent data directory with:

```text
OPENHOP_PLUGIN_DATA=/var/lib/openhop/plugins/openhop.nomad/data
```

The plugin owns everything below that directory. By default it uses:

```text
$OPENHOP_PLUGIN_DATA/
├── config.json
└── nomad_sessions.json
```

Persistent conversations are disabled in v0.1.3 because the upstream session lifecycle cannot
yet be bounded safely. `one_shot` must remain `true`; the session-map setting is retained only
for configuration compatibility.

When `OPENHOP_PLUGIN_DATA` is not set, existing standalone behaviour is preserved and the session map defaults to `./data/nomad_sessions.json`.

## config.json

With `OPENHOP_PLUGIN_DATA` set, the plugin reads `$OPENHOP_PLUGIN_DATA/config.json` if it exists.

Minimum configuration:

```json
{
  "nomad_url": "http://192.168.0.170:8080",
  "nomad_model": "qwen2.5:3b-instruct",
  "allowed_sender_prefixes": ["001122334455"]
}
```

Typical configuration:

```json
{
  "meshcore_host": "127.0.0.1",
  "meshcore_port": 5001,
  "nomad_url": "http://192.168.0.170:8080",
  "nomad_model": "qwen2.5:3b-instruct",
  "nomad_collection": null,
  "nomad_timeout_seconds": 120,
  "one_shot": true,
  "max_concurrent_requests": 1,
  "max_pending_requests": 1,
  "max_requests_per_sender": 2,
  "max_requests_global": 4,
  "rate_limit_window_seconds": 60,
  "allowed_sender_prefixes": ["001122334455"],
  "busy_wait_seconds": 5,
  "max_reply_chunks": 4,
  "max_chunk_bytes": 80,
  "reply_chunk_delay_seconds": 2.0,
  "max_prompt_bytes": 1000,
  "radio_prompt_enabled": true,
  "duplicate_ttl_seconds": 600,
  "log_level": "INFO"
}
```

Configuration precedence is:

```text
built-in defaults
    < config.json
    < environment variables
```

An empty `allowed_sender_prefixes` list denies every MeshCore sender. Set it to the exact
12-character sender prefixes that may use NOMAD. `one_shot` must remain `true`. The default
limits permit one active request, two requests per sender per minute, and four requests
globally per minute.
Rejected overload and authorization traffic is dropped without an RF reply. NOMAD HTTP
responses are capped at 256 KiB, redirects are not followed, and the configured timeout is
an end-to-end request deadline implemented without non-cancellable worker threads. `NOMAD_URL`
must use an IP literal so DNS resolution cannot outlive that deadline.

This keeps environment variables available for development and existing standalone deployments.

Important environment overrides include:

- `MESHCORE_HOST`
- `MESHCORE_PORT`
- `NOMAD_URL`
- `NOMAD_MODEL`
- `NOMAD_COLLECTION`
- `NOMAD_TIMEOUT_SECONDS`
- `ONE_SHOT`
- `NOMAD_SESSION_MAP_PATH`
- `MAX_CONCURRENT_REQUESTS`
- `MAX_PENDING_REQUESTS`
- `MAX_REQUESTS_PER_SENDER`
- `MAX_REQUESTS_GLOBAL`
- `RATE_LIMIT_WINDOW_SECONDS`
- `ALLOWED_SENDER_PREFIXES` (comma-separated 12-character hexadecimal sender prefixes)
- `NOMAD_BUSY_WAIT_SECONDS`
- `MAX_REPLY_CHUNKS`
- `MAX_CHUNK_BYTES`
- `REPLY_CHUNK_DELAY_SECONDS` (0–60 seconds between multi-packet reply chunks)
- `MAX_PROMPT_BYTES`
- `RADIO_PROMPT_ENABLED`
- `RADIO_PROMPT_TEMPLATE`
- `DUPLICATE_TTL_SECONDS`
- `LOG_LEVEL`

`NOMAD_URL` and `NOMAD_MODEL` must be provided by `config.json` or environment variables.

## Plugin manifest

`openhop-plugin.json` declares this as a simple Python service plugin:

```json
{
  "schema": 1,
  "id": "openhop.nomad",
  "name": "NOMAD Bridge",
  "version": "0.1.3",
  "runtime": {
    "type": "python",
    "entrypoint": "meshcore-nomad-bridge"
  }
}
```

There is deliberately no UI, permission model, Docker runtime or Repeater-internal hook in this plugin package.

## Standalone development

The plugin remains runnable without the plugin manager:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]

export NOMAD_URL=http://127.0.0.1:8080
export NOMAD_MODEL=qwen2.5:3b-instruct
meshcore-nomad-bridge
```

## Testing

```bash
python -m pytest
```

## Build wheel

Build a distributable wheel from the plugin root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip build
python -m build --wheel
```

The wheel is written to `dist/`, for example:

```text
dist/openhop_nomad_plugin-0.1.3-py3-none-any.whl
```

If you want both wheel and source distribution, run:

```bash
python -m build
```
