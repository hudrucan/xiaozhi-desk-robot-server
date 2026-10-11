# Distributed conversation inactivity timeout

The transport core uses the shared `close_connection_no_voice_time` setting
(default 120 seconds) to end a silent conversation. It keeps the MQTT connection
independent of the audio/core session: firmware receives terminal TTS stop and
goodbye, closes its audio channel and returns to idle. A new wake can create a
fresh conversation normally. Existing `app.py` behavior is unchanged.

VAD-confirmed speech, nonempty ASR partial/final text and explicit user input
reset activity. Raw Opus silence, ASR admission/recovery, worker leases and ping
do not. A worker sends an owned, metadata-only `speech` hint at most once per
second. Old core clients ignore this additive hint; old workers still supply
text activity to upgraded cores. Upgrade all workers for complete VAD tracking.

LLM, tool and TTS processing cannot be interrupted by inactivity expiry. The
inactivity period starts again after processing ends. On expiry the core cancels
listening, claims termination once and prevents late listen/audio messages from
reopening the ending session. Disconnect cancels the watchdog and owned work;
active-session status is removed before awaiting cleanup.

With configured `end_prompt.enable`, the closing instruction uses the existing
LLM/TTS transport with a 30-second LLM budget and no tool or Memory operations.
It is not inserted as a spoken-user transcript. Disabling the closing prompt
ends immediately. Legacy session bundles remain readable with timeout defaults;
new exports carry the timeout and closing prompt from shared Cloud configuration.

Upgrade core, worker, control-plane and runtime-apply source before exporting
new session fields: older strict bundle validators reject them. Runtime Apply
then activates the saved Cloud values. Provider models and firmware need no
changes.

Focused offline checks:

```bash
cd main/xiaozhi-server
.venv/bin/python -m unittest discover -s tests -p 'test_conversation_idle.py'
.venv/bin/python -m unittest discover -s tests -p 'test_voice_transport.py'
.venv/bin/python -m unittest discover -s tests -p 'test_worker_asr.py'
```

Operator check after rollout: wake, finish a response, then remain silent for the
configured interval. The robot should return to idle and release its core/ASR
session. Continuous speech, normal conversation and a tool/TTS response should
not trigger an inactivity close. Real microphone/VAD accuracy and physical
playback timing still require firmware verification.
