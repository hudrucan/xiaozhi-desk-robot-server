# Distributed streaming ASR and text LLM

This page describes the base text-only composition. The separate explicit
[parallel segment TTS opt-in](cluster-worker-tts.md) extends it with audio output;
without that opt-in, the behavior documented below remains text-only.

This explicit opt-in adds worker-local Opus decode, Silero VAD and transcription
to the existing Core NATS worker. Each speech turn is admitted by one queue
subscriber and stays on that worker until it finishes or fails. LLM runs as a
separate queue-balanced stage. `app.py`, normal ASR adapters and ping-only worker
behavior remain available; neither gateway placement nor VIP policy changes.

The Cloud-selected adapters supported here are Sherpa streaming (local CPU) and
Gemini streaming. Sherpa uses checksummed existing encoder/decoder/joiner/token
files; it does not need an ASR API key. Gemini uses the selected node-local key,
model/language/mode through an isolated async adapter. Its manual activity
boundaries and final/interim transcript fields follow the
[official transcription interface](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe).
There is no fallback that silently changes the selected provider.

## Immutable provisioning

`export_worker_asr_config.py` validates the existing shared V2 desired cache and
exact expected revision, resolves only ASR credentials when needed, and hashes
existing local model assets. It performs no Drive I/O, Cloud publication,
Soundbank materialization or active-runtime marking. Source assets are required
under the provisioned control-plane source root. Missing assets, mismatched VAD
checksum or unsupported configuration fail before bundle publication.

The worker requires the original NATS variables, `XIAOZHI_WORKER_LLM_CONFIG` and
`XIAOZHI_WORKER_ASR_CONFIG`. Both private immutable bundles must have matching node
identity and revision. The optional `XIAOZHI_WORKER_ACTIVITY_PATH` names an existing
private runtime directory's `activity.json`. Run `worker_asr.py` in place of the
existing worker process, not alongside another worker with the same identity.

Use `requirements-worker-asr-local.txt` for Sherpa or
`requirements-worker-asr.txt` for Gemini, plus system libopus. These extend the
minimal LLM requirements; the full server dependency set is not installed.
The worker loads/checks required model assets before subscribing and opens a
provider stream only when speech begins (or the first manual audio frame).

An immutable bundle does not hot-reload when Settings changes. Re-export/restart
workers and align `XIAOZHI_CORE_ASR_REVISION` before accepting the new revision.
Until TTS and MCP exist, hello/status advertise only `voice_text`, while
`conversation_runtime` remains false.

## Stream contract and ownership

- Protocol `xiaozhi-asr-stream-v1`.
- Admission `xiaozhi.v1.asr.open`, queue `xiaozhi-asr-workers`.
- Targeted input `xiaozhi.v1.asr.<worker_id>.input`, no queue group.
- Core-generated private result inbox, subscribed/flushed before admission.
- Worker-generated random ownership token; correlated worker/turn/revision on
  every acknowledgment and result. No provider configuration or credentials in
  NATS messages.

Audio packets have a two-byte header length, bounded JSON metadata and raw Opus.
They are not base64 PCM. Audio sequence must match exactly; an invalid gap fails
the turn. Admission is bounded to three seconds; each input acknowledgment to
two seconds. A lease is refreshed every second and expires after four seconds;
its independent watchdog also cancels a blocked provider operation. Each ASR admission
expires after at most forty seconds, with thirty seconds captured audio and five
seconds finalization. Two turns per worker, 64 input frames per turn, 4 KiB per
Opus frame and 16 KiB UTF-8 per transcript bound memory and work admission.

The negotiated profile is mono 16kHz, 60ms Opus. Manual mode ends on listen/stop;
auto mode uses worker-side VAD based on decoded samples, with configured Silero
thresholds/silence duration and ten frames of pre-roll. Realtime listening is
explicitly unsupported. Native inference is owned and joined before release;
native code cannot be forcibly interrupted like an async network call.

Core NATS is at-most-once. No replay, JetStream or speech storage exists. While
the client is still Listening, an expired/unavailable/busy/failed provider admission
is released and the core opens a fresh queue-balanced ASR admission with capped
0.25–2 second backoff. MQTT, the gateway/core session and listening generation
remain unchanged. An empty auto-mode transcript also reopens ASR without completing
the listening turn. The 30-second decoded-audio bound is an expiry, not malformed
audio; idle audio therefore does not permanently disable recognition.

This resets recognition, not the conversation. Old partial text is cleared and
never passed to LLM. Only ten recent Opus frames are retained during recovery;
audio already accepted by a lost worker is not replayed and the user may need to
repeat an interrupted utterance. Explicit listen/stop, abort, new listen/start or
transport disconnect ends/cancels recovery. Revision mismatch and malformed input
remain terminal errors; LLM/TTS jobs are not automatically retried. Successful ASR
final delivery does not wait for the worker's independently owned provider cleanup.
NATS sequence checks detect gaps after core
forwarding. The current gateway filters duplicate/out-of-order UDP sequence but
does not forward the UDP sequence to core; this phase does not promise detection
of every packet lost before the gateway or lossless UDP audio.

## Robot and panel acceptance

Select the firmware's existing Xiaozhi ASR mode to send microphone audio through
the cluster; direct Gemini mode intentionally bypasses cluster ASR. Core handles
listen/start/stop, wake notification, abort and disconnect. Wake text is not a
user LLM request. One session-owned task chain emits stt partial/clear/final,
then the existing bounded LLM RPC receives nonempty final text only. It emits
explicit llm final-text and text-only completion messages; no fake TTS lifecycle
or audio is generated. There is no conversation history, tool execution or TTS.

Firmware needs the accompanying bounded llm-text display and scheduled
Listening-to-Idle completion handling. User builds/flashes and verifies this
change; code review or loopback fixtures are not hardware validation.

Activity snapshots contain only node/instance/boot identity, monotonic freshness
and ASR/LLM counts. Clock means an admitted ASR turn (including preparation and
finalization); Play blinks for an active LLM job. Pause requires a fresh confirmed
idle worker. Missing/stale telemetry shows no compute icon. ASR may be lit while
waiting for speech; this does not claim that the provider has already opened.
Role digits nt/Co/iP, node number and colon-off policy remain unchanged.

## Validation

Offline fixtures cover queue selection/pinned routing, frame bounds/order,
ownership, revision, lease expiry, cancellation, actual Opus decode, silence and
sample-based VAD, private cache-only exports, loopback gateway framing,
transcript-to-LLM and transport-only compatibility. Existing LLM/ping and panel
fixtures remain applicable. Provider adapters do not import legacy server ASR
base classes, ConnectionHandler or provider initialization from app.py.

The explicit `probe_worker_asr.py` requires a user-provided 16kHz mono PCM16 WAV
up to thirty seconds. It sends paced manual audio through Core NATS and prints
safe result metadata; transcript printing requires `--show-text`. Run it only
after the chosen provider, immutable source pin and model/bundle checks pass.
Offline fixtures do not prove real-model accuracy, ARM64 dependency availability
or robot behavior. Canary speech recognition must pass before all-node rollout.
