# Distributed Desk camera analysis

The combined `worker_asr.py` runtime can run the selected Gemini VLLM adapter.
The normal `app.py` Vision endpoint and provider behavior remain unchanged.
No camera firmware change is required.

The firmware camera tool remains `self.camera.take_photo`. MCP initialization
advertises `/mcp/vision/explain` on the applied management VIP and an opaque,
session-scoped token. Any control-plane HTTP backend can relay an upload to the
core owning that session using its deployment-configured peer map and private
core port. The control plane imports no VLLM provider and runs no model.

The core accepts an upload only during that session's camera tool call, with
matching Device-Id, Client-Id and question. A call consumes at most one upload.
The existing multipart `question` / `file` contract and JSON result
`{"success":true,"action":"RESPONSE","response":"..."}` are retained.

## Worker selection and limits

The core makes a queue-group admission request on `xiaozhi.v1.vision.admit`
with queue `xiaozhi-vision-workers`. One worker is selected for the whole image.
Acknowledged image chunks and inference stay on
`xiaozhi.v1.vision.<worker_id>`, without a queue group. Chunks are 128 KiB,
so no image requires an increase to NATS's normal maximum payload.

Uploads are bounded to 5 MiB JPEG, a 2048-byte UTF-8 question, ordered offsets
and a SHA-256 digest. The public JSON reply is bounded to 3500 bytes so the
firmware can serialize it again inside its existing 8 KiB MCP envelope.
No image is written to disk or included in logs. NATS carries image/question
content for inference, never API keys or provider configuration.

HTTP upload parsing has a three-second budget; the NATS operation has a
15-second total budget and provider inference at most 12 seconds. The VIP relay
is bounded to 19 seconds, below the firmware camera HTTP timeout of 20 seconds.
Each worker reserves at most one vision job and shares the existing two-job
language/vision compute budget. The existing blinking play indicator represents
this compute activity; no panel schema or role symbols change.

Abort, session disconnect, request cancellation, NATS disconnect and expiry
release the reserved job. Image upload/inference is not replayed automatically
on another worker after uncertain delivery. A later camera call can select a
worker afresh. No JetStream or persistent image store is used.

## Cloud configuration and activation

`selected_module.VLLM` selects the existing Cloud `VLLM` configuration.
This adapter currently supports `type: gemini`, preserving model, language,
thinking level, media resolution and generation options. Other adapters and
proxy deployment require separate implementation. The VLLM API key can be
provisioned using the existing Providers key field and all-node secret workflow;
the LLM key is not implicitly reused.

The cache-only LLM export adds an optional private `vision` object. The TTS/core
export adds `vision_enabled`. Missing/unconfigured VLLM credentials disable the
camera capability and omit the camera tool; they do not disable ordinary voice
or reuse another provider's key. Invalid configured options fail validation.
Runtime Apply stages these bundles with the same Cloud revision and preserves
the existing CAS/rollback lifecycle.

Upgrade the worker, core, control plane and runtime-apply source on all nodes
before exporting/applying the new bundle fields: older strict bundle validators
reject them. After Runtime Apply, reconnect the firmware conversation to obtain
the new MCP initialization URL/token. Source deployment alone does not activate
Vision in an old immutable bundle. No additional dependency is required beyond
the combined voice worker's existing Google GenAI SDK and HTTP/NATS packages.

Settings Logs receives safe `vision_admitted`, `vision_complete` and
`vision_failed` events with the selected worker identity. API keys, image bytes,
questions, tokens and raw provider exceptions are excluded.

Focused checks use fake HTTP/NATS/provider objects:

```bash
cd main/xiaozhi-server
python -m unittest discover -s tests -p 'test_cluster_vision.py'
```

Real firmware camera capture and live provider analysis still require an
operator smoke check after deployment; unit checks do not establish hardware
compatibility or end-to-end latency.
