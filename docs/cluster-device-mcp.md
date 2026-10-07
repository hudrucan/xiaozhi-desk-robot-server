# Device MCP in the cluster voice runtime

The opt-in ASR/LLM/TTS core supports firmware-advertised MCP tools. Normal
`app.py` and its tool manager are unchanged. The default transport/ping-only
processes remain provider-free and do not execute tools.

After replying to gateway hello, a core with a client `features.mcp: true`
initializes MCP and discovers every tool page in a background task. The gateway
may answer discovery from its firmware-confirmed connection cache; its cached
responses use the same name cursor and 8 KiB MQTT page limit as direct firmware
responses. Discovery is bounded to ten seconds, 128 tools and a 224 KiB inventory.
There is no process-global or persistent device inventory. An incomplete,
ambiguous or failed inventory cannot silently become an executable tool list.

The core maps model-safe aliases back to exact firmware names. It requests the
normal assistant inventory, without enabling user-only firmware tools. Tool
schemas and names are dynamic; no Desk Robot actuator list is hardcoded.

## Turn ownership and continuation

`xiaozhi.v1.llm.tools.stream` uses queue group `xiaozhi-llm-workers`. Its bounded,
versioned protocol is `xiaozhi-llm-tools-v1`. The original final/text-stream RPCs
and empty-capability worker ping contract remain unchanged.

A single admitted worker owns the whole LLM/tool loop. It streams ordinary text
into the existing parallel segment TTS coordinator. When it chooses tools, it
sends only correlated call IDs, names and JSON arguments to its owning core.
The core sends `tools/call` over that session's existing gateway/MQTT transport,
then replies to the worker's correlated NATS inbox with MCP text blocks and the
error flag. It preserves multiple calls/results in order. The worker performs
another model step with those results; a tool response is never assumed to be
the final assistant answer.

Gemini's full response Parts, including thought signatures, remain in the same
worker-local generator until continuation finishes. They are not reconstructed,
shared between turns or put on NATS. This follows the
[Gemini signed response continuation contract](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures).
SDK automatic function execution is disabled: only the owning core can call
this firmware session.

Limits are four tool rounds, eight calls total, twenty seconds per device call
and 120 seconds for the complete LLM/tool loop. NATS request payloads are bounded
to 256 KiB, tool events/results to 128 KiB, and each accepted device result to
14 KiB. Device calls are serialized in returned order because robot operations
can depend on preceding operations. Timeout or abort does not retry actuator
calls: a timed-out operation may already have run on hardware. Unknown aliases,
late/duplicate/foreign IDs and malformed results cannot execute another tool.
Disconnect or abort cancels the owned model stream and pending device requests;
Core NATS reconnection serves subsequent turns, without replaying old calls.

Explicit `listen/detect` typed input with `input_mode: text` uses this same
coordinator. The legacy long-text `web_chat` trigger remains supported through
the firmware's advertised `self.web_chat.consume_pending` tool, preserving the
full original message. Unmarked wake notifications retain their existing
transport-core behavior. No second chatbot or firmware transport was added.

## Diagnostics and boundaries

The existing private `/diagnostics` and Settings Logs report `mcp_ready`,
`mcp_unavailable`, `mcp_call_started`, `mcp_call_complete` and `mcp_call_failed`.
The MCP flag indicates whether a live session has a complete inventory. Logs
contain lifecycle metadata only, not arguments, results, transcripts, secrets
or provider signatures. After disconnection the session and its inventory are
removed.

This is device MCP, not external/server MCP or distributed plugin execution.
There is no new Vision HTTP endpoint/camera upload capability, provider-test UI,
worker discovery database or cross-turn conversation history. Camera/VLM tools
that require a Vision endpoint still need that separate integration. Provider
selection and runtime revision use the existing immutable bundles; this code
change does not apply a pending Cloud revision.

## Minimal firmware verification

After all workers and cores run the new source, reconnect the client and wake it.
Settings Logs should show `mcp_ready`. Ask the robot to read its status or volume;
confirm a call-complete event followed by the assistant's answer. Then verify a
multi-action turn, abort while a tool is pending, and a reconnect. Test the
existing long Web Chat bridge with more than twelve Unicode codepoints. Build,
flash and physical tool validation remain user-owned.
