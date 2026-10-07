# Standalone cluster control plane V1

`main/xiaozhi-server/control_plane.py` runs the same small HTTP/control process on
each node. There is no leader identity. It serves Settings assets, desired Config
GET/PUT/sync, explicit migration preview/apply, `/api/cluster` and `/healthz`.
It does not import the conversation HTTP/WebSocket composition, initialize any
VAD/ASR/LLM/VLLM/TTS provider, resolve secrets for those providers, or select/mark
conversation runtime as active. `app.py` retains its existing behavior.

## Install and explicit process configuration

From `main/xiaozhi-server`, create a separate environment for this service:

```bash
python3.11 -m venv .venv-control-plane
.venv-control-plane/bin/pip install -r requirements-control-plane.txt
```

The minimal set includes HTTP, YAML/locks, Drive authorization/requests, Core NATS
and the codec used to validate retained P3 Cloud assets during Config Save. It
contains no provider SDKs or model runtimes. Native `libopus` is needed when
validating P3 assets, but no encoder/provider is loaded merely to start Settings.
V2 Settings patches without `static_soundbank` preserve existing asset layers and
pointers without local bytes or Soundbank publication. Structural, pointer/path
and audio-contract metadata validation still runs for every node. Patches containing
`static_soundbank` and V1/legacy saves retain the existing local-byte ownership and
publication rules; this process does not generate or materialize runtime audio.

Use the node's already-provisioned `data/bootstrap.yaml` with
`config_provider: google_drive`. Node identity follows its bootstrap/hostname
semantics, independently of `XIAOZHI_WORKER_ID`. Existing V1/legacy Config remains
readable and keeps its original Settings scope until explicit migration.

Required NATS environment:

```bash
export XIAOZHI_NATS_SERVERS='nats://10.10.10.11:4222,nats://10.10.10.12:4222,nats://10.10.10.13:4222'
export XIAOZHI_NATS_USER='xiaozhi'
# Provide XIAOZHI_NATS_PASSWORD using private node-local service configuration.
export XIAOZHI_CONTROL_PLANE_HOST='127.0.0.1'
export XIAOZHI_CONTROL_PLANE_PORT='8004'
export XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE='false'
export XIAOZHI_CONFIG_RECONCILE_SECONDS='45'
.venv-control-plane/bin/python control_plane.py
```

The NATS application username must match the cluster role. Credential-bearing
URLs are rejected, and credentials are never logged or published. HTTP bind and
access policy are process-local environment values, not inherited from
`server.ip` or Cloud runtime settings. Host must be an IPv4/IPv6 literal, port
1–65535, remote policy `true`/`false`, and interval 1–3600 seconds. Defaults are
loopback, port 8004, loopback requests only and 45 seconds. Future LAN/HA deployment
must explicitly set both the bind and remote access policy. Forwarded headers
do not grant access. HTTP request bodies are limited to 256 KiB.

SIGTERM/SIGINT stop HTTP, finish owned configuration I/O, detach publication
observation, stop background tasks and drain/close NATS. Shutdown does not depend
on stdin and performs no runtime restart or active-state write.

## Cloud authority and non-authoritative hints

The existing immutable Config object and revision/ETag CAS-last Drive manifest
remain the sole authority. After a successful V2 Config publication in the
control plane's store, an observer queues one best-effort message on
`xiaozhi.v1.config.changed`:

```json
{"protocol":"xiaozhi-config-v1","revision":12}
```

The maximum message is 256 bytes with a positive signed-64-bit revision. No Config
body, secret reference, Drive identity or credential travels over NATS. A failed
queue/publish/flush never rolls back or fails an already committed Save. V1 Save
does not emit a shared V2 hint; an explicit V1→V2 migration does.

Each node subscribes **without a queue group**. A newer revision hint wakes one
serialized live refresh; duplicates/older hints and malformed/oversize payloads
are ignored. Bursts coalesce to one pending highest revision. The downloaded
manifest/object must pass the existing checksum, topology, all-node validation,
rollback and revision-reuse guards. A hint's number is never adopted as state.

Live refresh also runs at startup, every configured interval, and on NATS
reconnect. Core NATS is at-most-once: periodic refresh repairs missed/dropped
hints and discovers edits made by CLI or the normal `app.py` process, which has
no new NATS runtime dependency. Initial connection failures and unexpected close
retry; established connections use all supplied servers and indefinite reconnect.

Only desired cache/status changes. Conversation runtime is not hot-applied.
The process observes an existing validated active disk snapshot during refresh
and reports it as the **last recorded conversation startup**, with
`runtime_revision: null`. It cannot attest that another process is currently
running. It never writes `active.json` or calls `mark_applied()`.

## Soundbank audio synchronization

Every shared V2 desired snapshot also schedules an independent background audio
cache reconciliation on that node. The node that accepted a successful Settings
CAS schedules its own sync directly, even if NATS publication fails. Other nodes
refresh from Cloud on the revision hint, at startup, periodically (45 seconds by
default), or on reconnect. Each downloads missing blobs directly from Drive;
neither audio nor Cloud file IDs are sent over NATS. An offline node catches up
after recovery. V1/legacy sources retain their previous behavior.

SHA256, byte size and existing P3 audio contracts are verified before readiness.
All enabled entries must have published Cloud pointers. Blobs live in the private
content-addressed `data/cloud-soundbank/objects` cache. Only a complete verified
generation can atomically replace `data/cloud-soundbank/ready.json`; failed or
superseded transfers leave the previous complete index and blobs intact. Runtime
Soundbank filenames and `active.json` are never overwritten. Background sync
neither generates audio nor initializes providers. Downloads are bounded to
32 MiB per asset / 256 MiB per snapshot and 4096 asset references. Old cache
generations are retained; automatic garbage collection is not provided yet.

Save success means the Cloud Config CAS committed, **not** that all three audio
caches are ready. Node download failures never reverse or fail an already saved
configuration. Sync failures retry automatically, including repair of a missing
local cache blob without any new Cloud revision. During shutdown the owned
download finishes (with the transport timeout), no further assets are fetched,
and no incomplete index is published.

`GET /api/cluster` includes a safe local `soundbank` status with desired/synced
revisions, counts, timestamps and fixed errors. `GET /api/cluster/soundbank` reads
the explicitly configured private control-plane peers concurrently and reports
readiness for the serving node's current desired revision. It uses the existing
three-node secret-provisioning topology, does not export its key or endpoints,
and performs no Drive I/O. Without that topology it explicitly reports
`local_only`, rather than claiming cluster readiness. Unreachable, incompatible
or old-version peers are unconfirmed, never counted as ready. The Cluster page
shows each node and the ready count. Unsaved edits are preserved during polling.

This adds **cache synchronization**, not distributed Soundbank playback or a
Soundbank authoring UI. Standalone authoring/preview controls remain unavailable;
normal `app.py` publication remains compatible and its Cloud edits are discovered
periodically. The TTS worker exporter still rejects enabled Soundbank until its
playback integration is implemented. Health remains the cheap Settings/config
policy below: a pending audio cache does not remove a usable Settings backend.

## Health and status

`/healthz` is unauthenticated under the explicit HTTP access policy and returns
only `{"healthy":true}` (200) or `{"healthy":false}` (503). It does no Drive,
secret resolution or filesystem I/O per request. Healthy means HTTP is operational
and the last validated usable **desired** snapshot contains the local node
assignment. A valid cached desired snapshot can keep HTTP healthy while Cloud
or NATS is temporarily unavailable; an active-only snapshot or missing assignment
is insufficient. It does not certify provider readiness, freshness, active VIP,
conversation availability or successful runtime apply.

`GET /api/cluster` reports local identity, desired and recorded active revisions,
sync/restart state, configured VIP, NATS state, reconciliation success/error
timestamps and fixed error codes, protocol/capability version and capability flags.
It exposes no credentials, raw exceptions, private Drive IDs, credential paths or
secret reference names. There is no presence database or `/api/cluster/nodes` yet.

## Standalone Settings limitations

The Cluster page shows shared/legacy scope, current node, revisions, sync/NATS
state, reconciliation timestamps and the configured VIP (`192.168.1.186` by
default). Polling status does not replace unsaved edits or their CAS base revision;
use Sync to load a newer desired configuration.

`GET /api/settings/capabilities` and Config responses explicitly identify
standalone mode. Runtime-only routes are not mounted: resource monitoring, logs,
restart, Push TTS, active Memory editing, soundbank generation/optimization/preview
and source switching. Their controls are hidden/disabled with a visible explanation.
Desired provider configuration remains editable without instantiating providers.
`/api/settings/restart` is absent: restarting this HTTP process is not cluster
apply. Rolling restart/distributed runtime control is not implemented.

Config V1/V2 migration and node-local secret rules remain as documented in
[Cloud Config](cloud-config-v1.md). V2 plaintext secret edits reject; pre-provision
named references on every member. Explicit node exceptions still take precedence.
Migration preview/apply is also available at `POST /api/settings/migrate-cluster`
using `nodes`, optional boolean `apply` and the required preview `base_revision`
for apply. This route performs no provider initialization or secret export.

VIP is shared **desired state only**. HAProxy, address assignment, unicast
Keepalived, failover and unified deployment remain the next iteration.
