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

The plugin reads `$OPENHOP_PLUGIN_DATA/config.json`. Conversation memory is RAM-only;
no conversation data is written to disk. The legacy session-map setting/path is accepted
for configuration compatibility but is not read, written, or deleted.

### Radio answer formatting

The default radio prompt asks for a direct, plain-text answer in one short paragraph,
ideally under 250 characters, without Markdown or numbered lists. It asks the model
not to invent sources or access instructions; accuracy and essential safety details
still take priority. This is guidance, not a guarantee of model output length or accuracy.
Existing saved/custom prompt templates are preserved: update the Radio prompt template
setting to adopt the new wording on an existing installation.

Inline bold/italic/code markers are removed before splitting. Sentence and paragraph
boundaries are preferred only when they fill at least 80% of the available text window;
otherwise splitting falls back to a word boundary. Multipart labels remain inside the
configured UTF-8 byte limit. When the maximum chunk count is reached, the last chunk
keeps answer text and ends with `...` instead of being replaced by a notice-only packet.

### Optional conversation memory

`one_shot: false` is the default. Leave **One-shot mode** unchecked
in the UI (or set `ONE_SHOT=false`) to remember each allowed sender separately:

- At most 10 complete question/answer pairs (20 messages), and 16,384 UTF-8 bytes
  counting role and content, per sender. Oldest complete pairs are dropped first.
- Each request keeps the newest question intact, removing history to fit the same
  16,384-byte message budget. An oversized wrapped question is rejected, never truncated.
  JSON framing/escaping and model tokenization are not included in this byte budget.
- At most 64 conversations, evicting the least recently successfully updated sender.
  Fixed 64 lock stripes also bound lock bookkeeping; rare hash collisions serialize
  unrelated senders without sharing their history.
- Memory expires 30 minutes after the last successful answer, including while idle.
  Restarting the plugin clears all memory. These limits are fixed safety constants.
- `/new` and `/reset` clear only the requesting sender, without inference. They use the
  same allowlist, admission, rate limits, global concurrency and RF pacing as questions.
  Ask/reset operations for each sender are serialized; a reset waits for an earlier ask.
- Only successful inference adds a complete pair. Errors/cancellation add nothing.
  An answer whose complete pair exceeds the budget is returned but clears that sender's
  retained history. Retained answers precede radio cleaning/truncation; Companion
  acceptance is not RF delivery, so memory does not imply the sender received an answer.

Both modes POST messages to `/api/ollama/chat` **without `sessionId`**; neither creates,
reads, or appends to NOMAD server chat sessions. This contract was checked against
Project NOMAD `85ad2d4bcd4c91c4ffc2c7a07c832dada0a9855f`,
`admin/app/controllers/ollama_controller.ts` and `admin/app/validators/ollama.ts`:
chat messages are accepted directly and chat-database writes require `sessionId`.
This does not promise that the upstream service has no other telemetry/logging.
Existing remote sessions and local legacy maps are left untouched, not migrated or deleted.

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
  "one_shot": false,
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

Older configurations may omit newer limits. When absent, `max_pending_requests`
defaults to at least `max_concurrent_requests`, and `max_requests_global` defaults
to at least `max_requests_per_sender` (with a minimum default of 4). Explicit
values still take precedence and inconsistent limits produce a validation error.
Loading never rewrites `config.json`; the UI presents compatible defaults and
persists them only when you intentionally save. Existing endpoints, access lists,
one-shot choices, and unknown settings are preserved.

Configuration precedence is:

```text
built-in defaults
    < config.json
    < environment variables
```

An empty `allowed_sender_prefixes` list permits anyone who can DM this Companion.
A nonempty list permits only the listed exact 12-character hexadecimal sender prefixes.
Blank entries are ignored; a list containing only blanks also permits everyone. `one_shot` defaults to `false`. The default
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

## Manual Companion adverts

The plugin version is `1.0.0`. Replacing an existing installation of the same
version requires an explicit reinstall; pip may otherwise keep the installed
package. Restart the plugin after an authorized installation: the version label
alone does not prove that the running process or UI assets were replaced.
The buttons use the running NOMAD plugin's existing Companion TCP connection,
not Repeater advert endpoints. Core 1.1.1 supports command 7 with flag 0
(zero-hop) or 1 (flood), replying OK or ERR. Acceptance is not RF delivery.
The command shares the client's command lock; uncertain acceptance closes that
connection and is never automatically retried.

The existing authenticated plugin settings API writes a fixed `advert_request`
envelope with `restart: false`. The plugin accepts only `zero-hop` and `flood`;
there is no shell command or additional HTTP/TCP listener. The existing
`/api/plugins/runtime?id=openhop.nomad` route reads the plugin-owned
`runtime.json` acknowledgement and one-use random challenge. Challenges expire
after 30 seconds, are consumed before sending, and are replaced on process start.
Thus saved config, reloads, restarts, and repeated submissions do not replay an
advert. Normal settings saves remove the transient envelope. The plugin never
writes config.json, so runtime acknowledgements do not overwrite settings.

This requires the manager's existing settings and runtime routes plus
`OPENHOP_PLUGIN_DATA`; controls stay disabled without a fresh connected runtime.
Only one operator should edit settings or submit an action at a time: the existing
settings API replaces the whole JSON document and has no compare-and-swap or
transactional action queue. The UI re-reads settings immediately before submitting
and preserves unknown keys, but concurrent editors can still overwrite one another.
A lost/replaced request expires; it is not retried. A missing acknowledgement means
unknown acceptance, not proof that nothing was transmitted. No RF test or deployment
is implied by building this wheel.

## Upgrading from v0.1.2 or v0.1.3 test builds

Install with dependencies (including `aiohttp` and `aiodns`). Python 3.10 and
newer remain supported. The `openhop-core==1.1.1` pin is unchanged. This release
promotes the tested 0.1.3 development line to 1.0.0; it does not migrate or
replace a saved configuration. Restart the plugin after installing the new wheel
and check the installed package version and running process, not just the UI label.
Before restarting, set `allowed_sender_prefixes` to the permitted 12-hex-character sender
prefixes and choose stateless (`one_shot: true`) or bounded RAM memory (`false`). An empty
allowlist permits everyone who can DM this Companion and logs a startup warning;
rate limits still apply. This changes the earlier deny-empty policy.
Old persistent-session maps are untouched and no longer used. Existing hostname/IP URLs remain usable subject
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
  "version": "1.0.0",
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

## Companion device controls and settings tabs

The configuration UI groups settings into Connectivity, Behavior & access,
Replies, Companion device, and Contacts tabs. Switching tabs preserves unsaved
edits. Use Tab to reach the tab list, Left/Right to switch, and Home/End for the
first or last tab; the tab buttons wrap on narrow screens.

In **Companion device**, first select **Read from Companion**, then choose
**Auto Add All or None**, **Overwrite Oldest**, and **Path hash bytes 1, 2, or 3**.
**Apply to Companion** changes the identity on the plugin's currently running
TCP connection, not the unsaved host/port fields. These are device operations,
not plugin configuration defaults; they do not restart the plugin. Normal
**Save and restart** saves bridge fields across all tabs, not these controls.
Existing Selected contact policies are retained unless All/None is explicitly
chosen. None clears selected contact-type bits but does not delete contacts or
restrict who can DM the bridge. Overwrite permits eligible old contacts to be
replaced when the table fills. Path width is per-hop hash size, not reply size.

The plugin uses its existing authenticated manager settings/runtime mailbox,
with process-scoped, expiring one-use requests and the shared command lock.
It reads preferences before modifying them, preserves unrelated flags, telemetry,
location, ACK and optional hop-limit values, and reads them back before reporting
verified success. No extra socket, dependency-pin change, startup preference
write, automatic retry, or rollback is introduced. Unsupported/disconnected or
stale runtimes disable controls. Partial, rejected, mismatched or unknown outcomes
are not success: read again before deciding whether to retry. Multi-command
updates are not atomic and affect other apps using the same Companion identity.
Manager settings writes replace whole documents without compare-and-swap;
avoid concurrent editors, saves, or device actions. Runtime polling does not
replace unsaved form values. Hardware/RF behavior requires separate validation.

## Contacts and dashboard access

The **Contacts** tab reads the running Companion's address book on demand. It
supports name/key search, type and favorites filters, persistent favorites,
and contact removal (with confirmation). Removing a contact changes the
Companion's address book; it is not merely hiding a row in the UI. Contact types
use Companion (1), Repeater (2), Room Server (3), and Sensor (4), with Unknown
(0) for unclassified entries. Displayed hop counts decode the path-length
byte's hash-width bits; `0xff` means the route is unknown.

The browser reads Repeater's `pymc_jwt_token` from same-origin localStorage and
sends it as a bearer token to the existing `/api/plugins/settings` and
`/api/plugins/runtime` routes. Repeater validates these requests server-side;
without a valid login, saved settings and contact actions return 401. The plugin
HTML, JavaScript, and CSS are still publicly readable at `/plugins/openhop.nomad/`;
do not put credentials in static assets or mistake a client-side disabled button
for authorization. Each contact action uses the plugin's running Companion
connection and a process-scoped, expiring one-use request; a write with unknown
outcome must not be retried automatically. Full-document settings writes have no
compare-and-swap, so avoid simultaneous editors.

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
dist/openhop_nomad_plugin-1.0.0-py3-none-any.whl
```

If you want both wheel and source distribution, run:

```bash
python -m build
```
