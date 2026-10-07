# Device MCP and server functions in the cluster voice runtime

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
another model step with those results. The explicit `handle_exit_intent`
direct-response contract is the exception: its configured farewell is already
the final answer, so no further model request or later actuator call is made.

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
full original message. Native wake notifications use the configured fixed acknowledgement described
below. No second chatbot or firmware transport was added.

## Server functions and session controls

The selected LLM worker adds the configured `function_call` Intent functions:
`get_current_datetime`, `get_weather`, `get_air_quality`, and `web_search`.
Location tools require `open_meteo`; search requires configured Tavily or Metaso
credentials. Disabled/unconfigured integrations are not advertised. Configured
descriptions and location options come from the same resolved Cloud desired
snapshot. Search credentials are resolved into the private node-local LLM bundle;
only declarations, call arguments and bounded results cross NATS. Arbitrary
plugin modules, stdio commands and filesystem paths cannot be selected by a
request. Weather/air-quality caches honor their configured TTL (minimum 60
seconds), with at most 64 entries per worker.

Mixed device/server calls execute in declared order, once, on the admitted
worker and owning device session. Server results continue the same model
stream, preserving Gemini response Parts/signatures locally. Server calls share
the twenty-second limit; replies larger than 14 KiB become fixed safe errors.
Abort cancels their async I/O; failures never retry an uncertain actuator call.
The original ping-only worker contract remains unchanged.

Cores always advertise the built-in `handle_exit_intent` tool. A successful call
speaks `exit_farewell`, sends TTS stop with `end_conversation: true`, then sends
`goodbye`. Exact normalized `exit_commands` end directly without asking the LLM,
matching normal app.py. Mentioning an exit command inside a longer sentence does
not directly end the conversation.

Native wake labels use the shared exact/legacy-label matcher and Cloud
`wakeup_words`, `enable_greeting`, and `wakeup_greeting`. The immediate firmware
`listen/start` does not cancel an acknowledgement: TTS stop allows the next
listen/start to open ASR. A disabled greeting leaves listening available without
a model call. ASR-recognized wake phrases honor
`enable_wakeup_words_response_cache`; ordinary typed input bypasses wake matching.
The existing segment/Soundbank path supplies acknowledgement audio; no provider
runtime is imported into the transport core.

LLM failures speak `system_error_response` through that turn's existing ordered
TTS stream, followed by normal playback completion. Firmware can resume ASR on
the same connection. A failed LLM/tool request is not replayed. If TTS itself is
unavailable, terminal error/cleanup remains bounded. Worker journal diagnostics
include only fixed exception categories, numeric HTTP status and tool-round
counts, never raw provider exceptions, credentials or tool contents.

New private bundles add optional `server_tools` to LLM and `session` plus
`system_error_response` to TTS/core bundles. Legacy bundles remain readable
(server functions disabled, reference session/error defaults). Upgrade **all**
worker/core/control-plane readers before exporting/applying the new fields;
older strict readers reject them. Apply the validated desired snapshot through
the existing coordinated runtime installer after the source upgrade, preserving
the Cloud revision and existing model/voice identity. A matching revision alone
does not retroactively populate these fields in an already installed bundle.

## Diagnostics and boundaries

The existing private `/diagnostics` and Settings Logs report `mcp_ready`,
`mcp_unavailable`, `mcp_call_started`, `mcp_call_complete` and `mcp_call_failed`.
The MCP flag indicates whether a live session has a complete inventory. Logs
contain lifecycle metadata only, not arguments, results, transcripts, secrets
or provider signatures. After disconnection the session and its inventory are
removed.

External MCP servers and endpoint clients are not mounted by this change.
Durable Memory tools still require a separate cluster ownership design.
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

Verify the configured wake acknowledgement, a date/time question, a configured
weather/search request, and a natural-language goodbye. After an induced model
failure, confirm the spoken error and the next ASR turn on the same connection.
A direct `exit`/`quit` should return to idle without a model request.
