# Distributed conversation and explicit Memory

The conversation core keeps bounded context for its own authenticated firmware
session. Each LLM turn includes up to seven completed user/assistant pairs and
its current user message. History is bounded to 24 KiB, with UTF-8-safe response
truncation. A failed, aborted or wake-only turn is not committed. Closing the
conversation discards this context; selecting a different LLM worker within the
same conversation does not discard it. Core failure does not replicate or
recover this transient history.

Durable Memory uses the selected `mem_local_explicit` configuration and the
existing Cloud Memory manifest/snapshot format. The deterministic provider logic
is shared with normal `app.py`; the standalone control plane does not import the
provider logger, initialize an LLM, or load conversation model runtimes.
`nomem` disables durable recall. `mem_local_short` is not supported by this
cluster implementation and fails export instead of silently changing semantics.

Each control plane subscribes to `xiaozhi.v1.memory.<node_id>` without a queue
group. The core derives the Memory scope from the authenticated `device-id`,
never from LLM arguments or a node ID. Ordinary turn recall reads the validated
local Cloud snapshot without Drive I/O. It preserves pinned/project memories,
recent-turn project resolution and the existing scoring/recall limits. The
small bounded recall result accompanies the LLM request as `memory_context`;
workers inject it into the configured system prompt's `<memory>` block.

`manage_memory` is advertised only when selected Intent is `function_call` and
its `functions` includes `manage_memory`. Remember, recall, forget and list use
the existing explicit-memory semantics. Ordinary conversation never creates
persistent memories automatically. Only final user/assistant text is retained
in transient history; tool call envelopes are not carried into later turns.

Shared V2 control planes have equal write rights. They use the same independent
Cloud Memory authority and CAS-last publication; there is no designated writer
or permanent leader. The first successful cluster mutation converts a legacy
Memory manifest to schema 2 (`write_mode: shared_cas`) in that same CAS. It keeps
all other device scopes and validated entries. Later mutations retain schema 2;
a downgrade is rejected even if its revision is higher. Config V2 and Memory V2
are separate formats with independent revisions.

Only a shared V2 control plane with a configured local node assignment enables
shared writing. Normal legacy `app.py` callers keep schema 1 ownership rules.
Updated legacy readers can read a shared Memory snapshot, but cannot write or
downgrade its manifest. Upgrade every control plane/reader before the first
cluster mutation; an old binary cannot parse Memory V2. VIP movement and the
former writer going offline do not change write permission. A missing common
manifest or unavailable Cloud service fails writes safely; it never creates a
second authority implicitly. Offline nodes can still recall validated cache.

Writes refresh the live manifest and retain the existing CAS-last publication,
conflict handling and pointer validation. A CAS conflict, timeout or lost reply
is not automatically replayed. An uncertain operation returns a tool error and
must not be described as a confirmed save/deletion. Request IDs are retained in
a bounded two-minute deduplication set; duplicates are not executed again.

After successful writes, `xiaozhi.v1.memory.changed` carries only protocol and
Memory revision. Every control plane receives the hint; startup, periodic
(default control-plane interval) and reconnect refreshes repair missed hints.
Hint failure does not invalidate a committed Cloud write. Standalone control
planes write only their private validated JSON cache, not local `.memory.yaml`;
normal `app.py` keeps its existing YAML materialization behavior. Memory state,
revision, sync state and write mode are visible under Settings → Cluster.
Settings → Memory lets the operator select a robot MAC/device scope and create,
read, update or delete entries through the VIP or any node's control plane.
Reads refresh Cloud (with validated offline cache fallback); mutations require
the browser's exact `base_revision` and fail with HTTP 409 on drift. Refresh and
review before retrying; the UI never silently rebases or retries a mutation.
Every save emits the same bounded hint as conversation tools. The editor shows
node sync counts using `/api/cluster/memory`; matching revisions require matching
opaque authority fingerprints, so different Memory sources cannot claim 3/3
sync. Fingerprints contain no raw Drive IDs. Counts are sampled cached readiness,
not a synchronous acknowledgement barrier. Offline peers catch up after recovery.
Memory changes take effect at the next recall without runtime restart; an
already running turn retains the context it captured.

The control-plane HTTP API uses `/api/settings/memory?device_id=<MAC>` for
GET/POST and `/api/settings/memory/<entry_id>?device_id=<MAC>` for PUT/DELETE.
Mutations carry `base_revision` in JSON, including DELETE. They use the existing
Settings loopback/explicit trusted-LAN access policy, bounded payload validation
and no-store responses. A missing provisioned source reports unavailable rather
than inventing local durable storage. Device scope is required for mutation.

The cluster `manage_memory` tool also exposes update/delete by exact `entry_id`
obtained from list/recall. Update preserves unspecified metadata; delete removes
only that device's entry. Remember remains an upsert/replacement operation and
forget marks matching entries inactive. The existing legacy tool schema and
normal app.py tool behavior are unchanged. Tool results return to the same LLM
stream before it generates the final response.

## Deployment prerequisites

All three control-plane bootstraps must point to the **same already provisioned**
`google_drive.memory_manifest_file_id`. Preserve/reconcile existing Memory
before assigning this pointer; do not create a separate manifest per node or
create another authority during routine rollout. Shared ownership migrates only
with the explicit cluster CAS path described above. The deployed control-plane
service must permit writing its `data/cloud-memory` cache (owned by its service
user). Neither Drive IDs nor credential paths belong in public status or NATS.

Upgrade all worker/core/control-plane readers before exporting the additive
`memory` binding in the private TTS/core runtime bundle or sending the optional
`memory_context` field. Apply a saved Cloud revision through normal Runtime
Apply, then reconnect the firmware conversation. Memory provider/policy changes
require matching applied bindings; a desired-policy mismatch fails safely until
Apply completes. Snapshot contents may advance independently of Config revision.
No new dependency or firmware change is required.

Offline regression: `python -m unittest discover -s tests -p test_cluster_memory.py`.
Real firmware verification: a follow-up question in one conversation; an
explicit remember followed by recall after reconnect/core selection; update,
forget and delete; CRUD through the VIP on different backend nodes;
and confirm that a failed write is reported as unconfirmed. These checks are
separate from automated fake Cloud/NATS tests.
