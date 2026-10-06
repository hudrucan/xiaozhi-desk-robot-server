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
Settings UI or management VIP frontend. There is no robot turn coordinator yet.

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

Offline checks cover wire bounds, prompt/revision ownership, queue/cancel
subscriptions, cancellation races, concurrency, provider failure, deadline,
stream close and credential-safe diagnostics. Actual SDK imports are checked
without full server/audio runtime imports. Existing transport/ping regression
checks remain applicable. Real Gemini calls and physical robot acceptance require
provisioned keys and the later conversation coordinator/ASR/TTS integrations.
