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

The expected default Companion endpoint is `127.0.0.1:5050`. Give NOMAD a dedicated Repeater Companion identity and bind its frame server to localhost when the plugin runs on the same host.

Example Repeater configuration:

```yaml
identities:
  companions:
    - name: "NomadBot"
      identity_key: "PRIVATE_IDENTITY_KEY"
      settings:
        node_name: "NOMAD"
        tcp_port: 5050
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

## NOMAD Docker networking

New plugin installations default to `http://nomad_admin:8080`. This is the NOMAD
container's DNS name and internal HTTP port, not the Repeater container's loopback.
The openHop Repeater container (which runs the plugin) and `nomad_admin` **must share
a user-defined Docker network** with the `nomad_admin` name/alias available there.
Docker's default bridge network does not provide this name-resolution contract.

Persist both containers' network attachments in their Compose/deployment configuration
so they survive container recreation and upgrades. For separate Compose projects, use
the same externally managed user-defined network in both configurations. A one-off
`docker network connect` is not a durable replacement for that configuration.

For standalone or remote deployments where `nomad_admin` is not resolvable, explicitly
set `nomad_url` in `config.json`, or override it with `NOMAD_URL`, to a reachable origin
such as `http://192.0.2.10:8080` or `https://nomad.example.org`. Use
`http://127.0.0.1:8080` only when NOMAD actually runs in the plugin's own network
namespace (for example standalone processes on the same host).
The Companion default is `127.0.0.1:5050`, matching Repeater’s initial Companion port. Existing explicit ports are preserved; set `meshcore_port` or `MESHCORE_PORT` to match your dedicated Companion endpoint if it uses another port.

## config.json

With `OPENHOP_PLUGIN_DATA` set, the plugin reads `$OPENHOP_PLUGIN_DATA/config.json` if it exists.

Minimum configuration:

```json
{
  "nomad_url": "http://nomad_admin:8080",
  "nomad_model": "qwen2.5:3b-instruct",
  "allowed_sender_prefixes": ["001122334455"]
}
```

Typical configuration:

```json
{
  "meshcore_host": "127.0.0.1",
  "meshcore_port": 5050,
  "nomad_url": "http://nomad_admin:8080",
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
  "radio_prompt_template": "You are answering a question received over a low-bandwidth MeshCore radio network.\\nGive the most useful answer first.\\n...\\n\\nUser question:\\n{question}",
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
an end-to-end deadline covering DNS, connection/TLS, headers and body reads. The transport
uses `aiohttp` with a dedicated `aiodns`/c-ares resolver, closing connections and cancelling
DNS queries on timeout or cancellation rather than leaving blocking Python `getaddrinfo`
executor jobs running. c-ares may use its own native event thread; this is not a guarantee
that the process creates no threads. It uses DNS/hosts resolution, not every OS NSS/mDNS
plugin: `.local` names require a DNS/hosts entry or an IP address where mDNS is unavailable.

`NOMAD_URL` accepts HTTP(S) hostnames (including Docker names such as
`http://nomad_admin:8080`), IPv4 and bracketed IPv6. Docker names only resolve when the
plugin container shares the appropriate network. Credentials, paths (except `/`), query
strings and fragments are not accepted in the configured origin. TLS certificates are
verified; environment proxies and cookies are not used. Repeated standard response headers
are supported. Parser limits are 8,190 bytes per header field/status line and 128 headers
and trailers; aggregate response headers are checked against 64 KiB after parsing.
Compressed responses are rejected (the request asks for identity encoding), so compression
cannot bypass the 256 KiB body cap.

All outgoing replies share `reply_chunk_delay_seconds` pacing, including short error
responses and concurrent answers. The interval starts when the previous send finishes;
there is no trailing sleep after the last packet. Companion retries remain internal to
`send_text` with their existing backoff. A rejected or unconfirmed chunk stops the rest of
that answer. `RESP_CODE_SENT` means Companion send acceptance, **not an over-air delivery
ACK**; retrying after an acceptance timeout can produce duplicates.

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
- `REPLY_CHUNK_DELAY_SECONDS` (0–60 seconds between outgoing reply sends, globally)
- `MAX_PROMPT_BYTES`
- `RADIO_PROMPT_ENABLED`
- `RADIO_PROMPT_TEMPLATE`
- `DUPLICATE_TTL_SECONDS`
- `LOG_LEVEL`

`NOMAD_URL` and `NOMAD_MODEL` must be provided by `config.json` or environment variables.

## Upgrading from v0.1.2

Install with dependencies (including the new `aiohttp` and `aiodns` requirements). Python
3.10 and newer remain supported. The `openhop-core==1.1.1` pin is unchanged.
Before restarting, set `allowed_sender_prefixes` to the permitted 12-hex-character sender
prefixes and ensure `one_shot` is `true`. An empty allowlist now denies everyone and logs a
startup warning; there is no public/open mode. Old persistent-session maps are not deleted,
but persistent mode is rejected at startup. Existing hostname/IP URLs remain usable subject
to the origin rules above. Review the conservative request limits and global reply pacing;
these intentionally restrict traffic compared with earlier versions. No automatic config
migration or deployment is performed. The new Docker URL is an installation default,
not an upgrade migration: preserve existing `config.json` files and environment
overrides; do not replace them with `config.default.json` during upgrades. Existing
explicit URLs, including loopback, remain unchanged. If an older loopback setting
is wrong for your Docker setup, change it deliberately after configuring the shared
network described above.

## Plugin manifest

`openhop-plugin.json` declares this as a Python service plugin with a dashboard UI for configuration editing:

```json
{
  "schema": 1,
  "id": "openhop.nomad",
  "name": "NOMAD Bridge",
  "version": "0.1.3",
  "runtime": {
    "type": "python",
    "entrypoint": "meshcore-nomad-bridge"
  },
  "ui": {
    "type": "application",
    "entry": "ui/index.html"
  }
}
```

The plugin remains a lightweight service package and does not add a custom permission model, Docker runtime, or Repeater-internal hook.

## Standalone development

The plugin remains runnable without the plugin manager. This example assumes NOMAD
runs on the same host/network namespace; otherwise set a reachable remote URL:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]

export NOMAD_URL=http://127.0.0.1:8080
export NOMAD_MODEL=qwen2.5:3b-instruct
export ALLOWED_SENDER_PREFIXES=001122334455  # replace with the permitted sender
meshcore-nomad-bridge
```

## Testing

```bash
python -m pytest
```

## Release automation

See [release and catalogue automation](docs/release-automation.md) for immutable publication, manual existing-release retries, credential boundaries, read-only proposal rehearsal, and the outstanding catalogue test gate. Catalogue-owned policy—not this producer—decides automatic merging.

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
