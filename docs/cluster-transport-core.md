# Cluster transport core

`main/xiaozhi-server/core_server.py` is an isolated transport core for the first
cluster MQTT acceptance phase. It does not import `app.py`, `ConnectionHandler`,
the existing WebSocket/HTTP server, setup UI, provider initialization, Cloud
configuration loading, or MCP execution. Normal `app.py` remains unchanged.

Each node can run this process. A session belongs to exactly one core until its
gateway WebSocket closes. The gateway selects a core other than itself and the
observed VIP owner where available; a remote VIP owner is the next fallback, then
the local core if remote session establishment fails. Equal candidates rotate
from a randomized initial position. This is session placement, not replication.

## Scope

This phase proves authenticated gateway hello, bounded incoming version-2 audio
framing, session ownership, cleanup, selection and panel telemetry. Incoming Opus
is counted and discarded; it is never decoded, stored, played back or sent to a
provider. No audio response, ASR, LLM, VLM, TTS or tool execution is implemented.
Unsupported requests get a bounded `conversation_runtime_unavailable` error,
suppressed after the first error until abort. The hello and status explicitly
report `conversation_runtime: false` and empty capabilities.

It does not serve bootstrap/OTA, Vision, Settings or worker RPC. The future
conversation coordinator and provider dispatch must be implemented before real
robot conversations can pass acceptance. Transport readiness must not be used
as evidence of conversation availability.

## Configuration and installation

Use Python 3.11 or later, from `main/xiaozhi-server`:

```sh
python3 -m venv .venv-core
.venv-core/bin/pip install -r requirements-core.txt
```

The full server requirements already pin the same aiohttp version. No models,
FFmpeg, Opus codec, NATS or Google Drive packages are required for this process.

| Environment | Contract |
| --- | --- |
| `XIAOZHI_CORE_ID` | Local identity, defaults to hostname helper; ASCII letters/digits/underscore/hyphen |
| `XIAOZHI_CORE_HOST` | Explicit private IPv4 bind address; default loopback |
| `XIAOZHI_CORE_PORT` | Unprivileged HTTP/WebSocket port; default 8000 |
| `XIAOZHI_CORE_AUTH_KEY` | Required, same key as gateway `SERVER_SECRET`; never logged |
| `XIAOZHI_CORE_GATEWAY_IPS` | Required comma-separated allowed gateway source IPv4 addresses |
| `XIAOZHI_CORE_MANAGEMENT_INTERFACE` | Required interface holding the management IP/VIP |
| `XIAOZHI_CORE_INGRESS_STATE_FILE` | Applied public ingress snapshot path; default `/etc/xiaozhi-ingress.json` |

After provisioning the environment, run `.venv-core/bin/python core_server.py`.
SIGTERM/SIGINT stops admission, closes sessions and cleans up the listener.
Do not commit environment secrets. Deployment runs with stdin disabled.

## HTTP and WebSocket contract

- `/xiaozhi/v1/?from=mqtt_gateway` accepts only an allowlisted source IP and the
  existing gateway HMAC bearer token, including device ID, client ID and a
  timestamp within five minutes. There is no unsigned fallback.
- The first message must be the existing version-2 WebSocket hello, within three
  seconds. Opus audio metadata is validated. The reply contains a fresh
  `session_id`, validated `audio_params`, `core_id`, empty capabilities and
  `conversation_runtime: false`.
- JSON is bounded to 8192 UTF-8 bytes; binary frames to 65507 bytes with the
  existing 16-byte gateway audio header and exact length. Pending and established
  sockets share a 128-connection admission limit. Duplicate hello is rejected.
- `ping` returns `pong`; goodbye/disconnect ends the session; abort resets only
  the unsupported-request notification because this process has no provider job.
- `/readyz` returns 200 while transport admission is available and 503 during
  shutdown. It performs no Cloud I/O and says nothing about provider availability.
- `/status` is private-network telemetry. It reports protocol
  `xiaozhi-core-transport-v1`, `core_id`, readiness, active established sessions,
  aggregate received frame count, capabilities and actual local VIP ownership.
  It exposes no device IDs, session IDs, secrets or backend URLs.

VIP observation uses the applied ingress snapshot and interface addresses once
per second. Unknown/invalid snapshot or failed observation reports null; gateways
exclude that node from new selection until ownership can be observed. No VIP is
pinned or assigned here. Observation and connection establishment are not atomic:
a VIP transition can briefly overlap the chosen session roles. Existing sessions
are never moved on ownership changes. A core/node failure requires a fresh session.

## Panel

The cluster panel polls its local core status. `C- NN` means the local core owns
an established session; `CP NN` additionally marks actual VIP ownership. Uppercase
C uses the top, both left and bottom segments. Idle remains `-- NN` or `iP NN`.
When fallback puts both gateway and core sessions on one node without VIP, the
panel shows `tC NN` continuously. With all three roles, it alternates `tC NN` and
`iP NN` once per polling cycle.
The colon remains disabled. Counts describe local roles across all sessions,
not provider execution or a single globally elected core.

## Operator acceptance

After reviewed source is deployed, verify private status identity and resolved
VIP observation on all nodes; authenticate a gateway hello; send a correctly
framed audio probe and check the core frame counter; verify selected core and
local panel count agree; end the session and check both counts clear. Repeat
with a changed VIP owner and unavailable preferred core to exercise fallback.
Malformed/oversize input and invalid authentication must be rejected. Confirm
provider requests report unavailable rather than producing a fake response.
Physical display, robot audio and failure recovery require operator validation.
