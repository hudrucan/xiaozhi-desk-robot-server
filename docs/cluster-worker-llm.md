# Standalone text LLM RPC

This opt-in phase proves bounded `core -> NATS -> text LLM worker -> core` RPC.
It does not enable robot conversations, ASR/VAD/TTS/VLM, memory, intent, MCP or
provider scheduling. `app.py` and the original ping-only `worker.py` remain
available. The original ping reply still has `capabilities: []`, including when
served by `worker_llm.py`. Transport hello still reports
`conversation_runtime: false`; receiving audio is not conversation acceptance.

## Local configuration and credentials

The worker reads an explicit immutable node-local bundle from
`XIAOZHI_WORKER_LLM_CONFIG`. It does not load full server config, call first-run
setup, contact Drive, initialize audio providers or read control-plane secrets
at runtime. Keep the bundle outside the repository, root-owned and readable only
by the worker's service group (`0640`), or owned by the worker with mode `0600`.
It contains the selected LLM credential and must never be logged or committed.

`export_worker_llm_config.py` is a separate cache-only provisioning operation to
run in the control-plane checkout/venv, after its Cloud desired snapshot and
local secrets have been provisioned. It uses the existing cache checksum,
manifest/source/node validation and layer resolver, requires shared V2, resolves
only the selected LLM, and writes an atomic mode-600 bundle. It does not fetch
Drive, publish Config, mark runtime active or materialize Soundbank assets.
The destination directory must already exist and be private. No key or secret
reference is printed. Provision the worker ownership/group after export.
An immutable worker checkout can run the exporter with the existing control-plane
venv and `--source-root /opt/xiaozhi-control-plane/repo/main/xiaozhi-server`.
`--expected-revision` rejects Cloud drift before resolving credentials. Identical
private bundles are not rewritten. Export output contains only changed/revision/node metadata.

```bash
# Run from the control-plane server checkout, using its existing dependencies.
/path/to/control-plane/venv/bin/python export_worker_llm_config.py \
  --output /private/provisioning/worker-llm.json
```

Each bundle contains its node ID, exact desired revision, configured prompt and
selected provider settings. Gemini is the first adapter. Other selected provider
types, configured proxies or missing/unresolved credentials fail export/startup
explicitly. Request messages cannot select providers, supply credentials, override
the configured system prompt or supply tool schemas. The worker deadline is at
most 30 seconds; export limits the SDK timeout to this budget without modifying
Cloud Config. Configured max output tokens must be 1–8192, and the final UTF-8
text is capped at 64 KiB. Provider numeric/SDK settings are validated at startup.

Bundles are deliberately not hot-reloaded. After a Cloud revision change,
re-export and restart all text workers with the new bundles before probing the
new revision. A request for a different revision fails with
`llm_revision_mismatch`. This phase does not implement cluster rolling restart
or make the control plane's desired revision an active conversation revision.

## Process

Use the same three NATS URLs and application username/password as `worker.py`.
The required environment variables are `XIAOZHI_NATS_SERVERS`,
`XIAOZHI_NATS_USER`, `XIAOZHI_NATS_PASSWORD`, and `XIAOZHI_WORKER_LLM_CONFIG`.
Optional `XIAOZHI_WORKER_ID` retains hostname defaults and subject-token validation.

```bash
cd main/xiaozhi-server
python3 -m venv .venv-worker-llm
.venv-worker-llm/bin/pip install -r requirements-worker-llm.txt
# Configure the environment privately; do not put credentials in shell history.
.venv-worker-llm/bin/python worker_llm.py
```

Run `worker_llm.py` in place of the ping-only process on an enabled node; do not
run both under the same worker identity. The Ansible worker role defaults to
ping-only; its explicit LLM opt-in uses separately provisioned credentials/bundles.

Gemini reuses the existing adapter and generation settings with an added async
text-only interface. It uses the async SDK directly, with no background generator
thread or unbounded cross-thread queue. Cancellation closes its stream. Provider
imports no longer initialize global server logging; normal `app.py` still
configures logging before importing provider/server modules. Existing sync/tool
provider methods are retained. No tools or native search are requested by this
text-only interface.

## Wire and lifecycle

- Generate subject: `xiaozhi.v1.llm.generate`, queue `xiaozhi-llm-workers`.
- Cancel hint: `xiaozhi.v1.llm.cancel`, broadcast without a queue group.
- Protocol: `xiaozhi-llm-rpc-v1`.

Generate requests include a core ID, unpredictable request ID/cancel token,
integer config revision, absolute deadline and bounded user/assistant dialogue.
They contain no provider configuration or credentials. Requests are at most
32 KiB, with at most 16 messages (16 KiB each and a smaller aggregate bound).
Replies include request correlation, worker ID, revision and either bounded
final text or a fixed public error. Malformed/oversize/duplicate-key messages are
ignored safely; the worker replies only to a valid NATS `_INBOX.` reply subject.

Each worker owns at most two jobs. Busy/revision/expired requests fail without
provider work. Cancellation has a token ownership check and a bounded expiring
cache to handle cancel-before-delivery races. Core NATS remains at-most-once:
requests are never automatically retried, cancellation is best effort, and a
missed cancel self-terminates at the worker deadline. Worker results are never
buffered across NATS reconnect. SIGTERM stops admission, cancels jobs, closes
provider streams and drains NATS. Already completed jobs are not durable records;
this protocol does not promise exactly-once execution for manually replayed IDs.

The private core endpoint `POST /api/workers/llm` uses the same source allowlist
and gateway HMAC headers as transport probes. Body:

```json
{"revision":6,"dialogue":[{"role":"user","content":"Hello"}],"timeout_seconds":30}
```

The revision above is an example; use the exported bundle revision. The endpoint
has an 8 KiB body/read deadline and shares the eight-request core admission limit
with ping probes. It returns one final text response, not audio/streaming events.
Client disconnect/core shutdown cancels the owned call and emits a best-effort
cancel hint; no late result can advance a robot turn. It is not mounted in the
Settings UI or management VIP frontend. The separate opt-in voice-text coordinator
is documented in `cluster-worker-asr.md`; it now uses the streaming contract below.

## Conversation text streaming

`xiaozhi.v1.llm.stream` is a separate Core NATS subject using protocol
`xiaozhi-llm-stream-v1` and queue group `xiaozhi-llm-workers`. The original
`xiaozhi.v1.llm.generate` request/reply and private HTTP probe remain unchanged.
Both contracts share the two-job worker and eight-call core limits, the same
immutable provider/model/prompt bundle, revision/deadline checks and token-owned
cancel broadcast. This adds no TTS, tools, history or provider scheduling.

The core subscribes to a random, bounded `_INBOX.` before submitting a stream
request. One worker sends `started` (sequence 0), UTF-8 `chunk` events (sequence
1 onward), then `complete` with total UTF-8 byte count and SHA256. Each event
includes request ID, worker ID and exact configuration revision. Completion
contains metadata only; the core retains at most 64 KiB to send the existing full
`llm.final` message. Individual chunks are at most 4 KiB, event envelopes at most
8 KiB, and a turn has at most 4096 chunks and a 30-second total deadline. Large
provider chunks are split at character boundaries without changing their text.

Worker events use request/reply ACKs. Only one event may await ACK per job; the
worker does not advance the provider iterator until the core accepts the chunk.
ACKs have exact request/worker/revision/sequence correlation and a two-second
budget. The core consumer also has a two-second budget, a two-event local queue
and bounded NATS subscription pending limits. A future TTS consumer must apply
backpressure within this budget or explicitly redesign the bounded flow-control
contract; it must not accumulate unbounded text/audio queues.

The core rejects gaps, duplicates, worker changes, malformed/oversize events and
completion digest/length mismatches. It exposes partial text only while its turn
still owns the stream; partial text does not mean successful completion. Errors
after partial output invalidate the turn. No stream is replayed or automatically
retried on timeout, ACK failure or disconnect. Reconnect restores subscriptions
for new turns, not continuity of an interrupted stream. Cancellation closes the
provider iterator, removes the core inbox and releases admission. If a cancel
hint is lost, the worker ACK timeout/deadline bounds the remaining job lifetime.
Diagnostics do not print text, tokens, credentials or raw exceptions.

The voice-text core uses `WorkerRPC.generate_stream(..., on_chunk)` and emits
additive `llm.partial` events with zero-based `seq` and delta `text`, followed by
the existing full `llm.final` and `llm.complete` with `text_only: true`. Current
Desk firmware ignores partial events and displays the unchanged final message;
no firmware edit is required for this phase. Normal `app.py` stays unchanged.
The core receives text before LLM completion; segmentation and concurrent TTS
synthesis will be a later phase using the existing splitter policy.

Deploy streaming-capable workers before streaming-capable cores. Old workers do
not subscribe to the new subject, so a mixed rollout can route stream requests
only to upgraded workers; a cluster with no upgraded subscriber fails on the
NATS no-responder status or within the core deadline. There is no silent fallback
to a second full-response generation.
Do not change the active revision or Cloud provider settings for this transport
enhancement. Source/deployment updates remain explicit and serial.

`probe_worker_llm.py` sends an authenticated private HTTP request using
`XIAOZHI_CORE_AUTH_KEY` from the existing private core environment. It accepts only
the three private Desk IPs, disables proxies/redirects and bounds the payload,
response and deadline. Default output contains worker ID, revision, elapsed time,
text length and optional expected-text match; use `--show-text` only when needed.
`--cancel-after 0.3` closes the request to exercise cancellation; this result only
proves the client closed it. Check the core inflight count and the worker's fixed
`LLM job cancelled` / `LLM job released ... inflight=0` diagnostics for cleanup.
Diagnostics never include request text, API keys or cancellation tokens.

The cluster's explicit `worker-llm.yml` playbook opts into this entrypoint under
the existing `xiaozhi-worker` unit, with a separate immutable source/venv path.
It checks all three validated Cloud revisions, supports a one-node canary and
rolls selected workers serially. `verify-worker-llm.yml` runs the authenticated
probe without changing deployments. Normal full-site worker deployment remains
ping-only unless the LLM deployment variables are explicitly retained.

## Validation and next acceptance

Run the focused offline streaming and compatibility fixtures from an existing
provisioned Python environment (no real NATS/Drive/provider calls):

```bash
cd main/xiaozhi-server
PYTHONPATH=.:tests python -m unittest test_worker_llm_stream test_worker_llm \
  test_worker_rpc test_worker_asr test_voice_transport
```

The streaming fixtures cover early delivery before provider completion, ACK
backpressure, UTF-8 boundaries, order/identity/digest rejection, partial failure,
deadline, cancellation, shutdown and disconnect/reconnect ownership. A live
acceptance must separately verify one new voice turn and abort during partial
text after workers and cores have been explicitly rolled out. Current firmware
still shows the final answer, so visual output alone cannot prove early delivery;
observe `llm.partial` timestamps on the controlled transport or use a bounded
core callback recorder. Do not claim latency improvement from static review.

Offline checks cover wire bounds, prompt/revision ownership, queue/cancel
subscriptions, cancellation races, concurrency, provider failure, deadline,
stream close and credential-safe diagnostics. Actual SDK imports are checked
without full server/audio runtime imports. Existing transport/ping regression
checks remain applicable. Real Gemini calls and physical robot acceptance require
provisioned keys and the later conversation coordinator/ASR/TTS integrations.
