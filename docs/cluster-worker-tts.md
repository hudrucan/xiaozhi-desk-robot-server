# Parallel segment TTS within a voice turn

This opt-in extends the standalone `core_server.py` and `worker_asr.py` path.
Streamed LLM chunks feed the existing first-sentence/punctuation splitter on the
core. Up to three segments **of the same response** synthesize concurrently on
different available workers. Audio is played in segment order, regardless of
completion order. The normal `app.py`, Soundbank authoring and provider paths
remain available; this does not enable MCP, VLM or conversation history.

## Admission and audio ownership

Each worker has one warm native Sherpa synthesis slot. The core probes the
explicit bundle membership, excludes incompatible revision/voice fingerprints
and busy workers, then performs authoritative targeted admission. Among idle
workers it prefers its least recently selected node, with randomized ties;
node number and VIP ownership do not define TTS preference. Selection history
is local to each core; competing cores still share the worker's one-slot guard.
An already active ASR turn prevents TTS admission. ASR may start after admission,
so this is not an exclusive CPU scheduler or a latency guarantee.

The protocol is `xiaozhi-tts-segment-v1`, using
`xiaozhi.v1.tts.<worker_id>.segment` without a queue group. Requests carry only
bounded text and core/job/token/index/revision/fingerprint/deadline metadata.
`status`, `admit`, `poll`, `cancel` and `done` have exact validated envelopes.
PCM16 mono is pulled in at most 32 KiB chunks, correlated and SHA256 checked
before any of that segment reaches playback. No credentials, model paths or
provider configuration are transmitted over NATS.

At most three jobs/results are ahead of playback per turn, including the segment
currently playing. A segment is bounded to 512 Unicode characters / 2 KiB UTF-8,
30 seconds of audio and 3 MiB PCM; admission plus synthesis/pulls has a 45-second
deadline. Whole-turn text is at most 64 KiB / 512 segments, with a 120-second TTS
turn budget. Oversized segments fail explicitly rather than changing existing
split rules or accumulating unbounded audio. The lookahead bounds buffered
results to at most 9 MiB PCM per turn, with bounded temporary copies during pull
and encoding; native inference allocations and resident models are additional.

The core owns one response-wide Opus encoder and output PCM buffer. Resampler
state is preserved for consecutive segments with the same source rate and reset
when cached/native source rates differ. Output is mono 16kHz / 60ms Opus with the existing 16-byte gateway
header and paced delivery. There is one TTS start/stop lifecycle, original segment
subtitles, and one final frame-padding flush. Native model, voice, normalization,
word correction, volume/mastering and first-segment settings come from the bundle.

Only an unplayed, fully buffered segment can be reassigned after failure. Every
attempt has new ownership; partially transmitted audio is never replayed. Abort
or disconnect cancels all lookahead work. A lost cancellation expires via the
four-second poll lease. Native inference cannot be forcibly interrupted: a worker
keeps its slot and activity icon until the native call has joined, even if the
turn deadline has passed. A NATS disconnect permanently invalidates that core
turn, including already buffered results; reconnect serves new turns only.
No JetStream, durable job records or exactly-once synthesis are claimed.

## Explicit provisioning

Use Python 3.11 and system libopus. Core MP3 Soundbank fallback also requires
system FFmpeg; WAV and P3 decoding do not initialize provider runtimes. Workers extend their existing local ASR/LLM
environment with `requirements-worker-voice.txt`; cores use
`requirements-core-voice.txt`. The original minimal worker, ASR and core
requirement files remain unchanged. Provider/model dependencies stay on workers.

`export_worker_tts_config.py` reads only the existing validated shared V2 Cloud
desired cache and requires an explicit expected revision. It neither contacts
Drive nor changes desired/active state. It hashes the selected existing model,
tokens, optional model metadata and complete espeak-ng data tree. A canonical
fingerprint excludes node ID and installation root, so byte-identical assets and
settings match across nodes regardless of filesystem enumeration order.
Missing/mismatched assets fail startup; nothing is downloaded automatically.

Run export in the existing control-plane environment after provisioning assets:

```bash
cd main/xiaozhi-server
/path/to/control-plane/venv/bin/python export_worker_tts_config.py \
  --source-root /opt/xiaozhi-control-plane/repo/main/xiaozhi-server \
  --expected-revision "$DESIRED_REVISION" \
  --workers deskb1x,deskb2x,deskb3x \
  --output /private/provisioning/worker-tts.json
```

The destination directory must already be private. Export is atomic mode 0600;
provision service ownership/group and mode 0640 if needed. Each worker reads its
node-specific bundle via `XIAOZHI_WORKER_TTS_CONFIG`. Each core reads its matching
node-specific bundle via `XIAOZHI_CORE_TTS_CONFIG`; the core does not load model
bytes. TTS, ASR and LLM revisions must match `XIAOZHI_CORE_ASR_REVISION`. NATS
credentials retain the existing private environment configuration.

For enabled Soundbank, export requires the complete local control-plane
`cloud-soundbank/ready.json` to match the selected node, desired revision and
effective Soundbank/audio metadata. Every content-addressed blob is rechecked.
The optional `soundbank` bundle contains only ordered phrase/transcript entries,
hash/size/format contracts and a local cache root. It contains no Drive IDs,
secret references or credentials. Its separate fingerprint includes entry order
(normalized duplicates resolve first-wins), excluding installation root. Worker
voice fingerprints and the segment RPC contract stay unchanged; workers never
read control-plane Soundbank bytes.

Before enabling a core, `provision_core_soundbank.py` copies the frozen bundle's
verified blobs into a private immutable generation under
`/etc/xiaozhi-core-tts/soundbank/<fingerprint>`. Ansible stages/checks/renames the
complete generation, rewrites only the core bundle's audio root, and verifies
decode access as the unprivileged core user before activation. Existing
generations are verified without overwrite and retained. The core receives no
membership in the control-plane credential group. Provisioning uses local bytes
only, never Drive I/O, asset regeneration or provider initialization.

The core matches each segment with the same normalized text and first-segment
cached-prefix policy as the existing app. Hits use compatible mono/60ms optimized
P3 at 16kHz, otherwise canonical P3/WAV/MP3. Existing recorded `text` is the
subtitle when present. Audio is decoded to bounded mono PCM and fed through the
same ordered turn playback/Opus encoder as native results. P3 is re-encoded in
this composition, rather than transmitted byte-for-byte; gain/mastering is not
applied again. A cache hit consumes no native worker slot. A matching missing or
corrupt pinned asset fails with a fixed error before handoff; it is never silently
replaced with synthesis. Cache misses still use parallel worker segments.
Cancellation joins owned decode/subprocess work; NATS reconnect cannot revive
an old turn or late cached result.

Audio remains bounded to 30 seconds per segment, with private blobs limited to
32 MiB each / 256 MiB per bank and 4096 references. Decoding temporarily reads a
bounded compressed blob in addition to buffered PCM. Unsupported/overlong selected
audio fails provisioning before activation. Older bundles without Soundbank
remain readable. Normal `app.py` authoring/generate/optimize/cleanup behavior
is unchanged. Full-response buffering, cold-model mode and native debug logging
remain rejected for this segment worker.

Control-plane Save automatically syncs desired cache bytes on all three nodes.
Active cores keep their pinned generation until an explicit matching runtime
rollout; desired sync does not hot-apply providers or advance active revision.
This avoids changing recordings beneath an in-progress turn. Newer desired audio
and previous active audio can coexist; automatic cache garbage collection is
not implemented.

These variables are opt-in. Without them, the deployed ASR/LLM text-only behavior
remains unchanged. Opted-in core hello/status add `voice_tts` alongside
`voice_text`; `conversation_runtime` remains false because the full app runtime
and tools are still absent. Text-only errors/completion remain compatible with
existing firmware. The cluster repository provides separate `worker-tts.yml` and
`core-tts.yml` opt-ins, with explicit reviewed source pins and private vars. They
have not been activated by this implementation work. The worker playbook stages
and checksums missing model trees before atomic publication; existing trees are
verified without overwriting them. Check mode performs read-only preflight only.
Rollout must deploy the updated panel reader first, then matching models/bundles
and workers, and enable cores only after direct synthesis acceptance.

Worker activity adds an optional `tts` counter to the existing fresh local v1
snapshot. Updated panels accept both old and new schemas. Play stays lit during
native TTS, LLM blinks Play, ASR lights Clock, and a fresh idle worker lights Pause.
Role glyphs `nt/Co/iP`, node numbers and colon-off behavior do not change.

## User-owned verification and measurement

Offline fixtures cover simultaneous admission on three nodes, ASR/revision
exclusion, out-of-order completion with ordered playback, bounded lookahead,
unplayed failover, corrupt PCM, cancellation before/after native start, permanent
disconnect invalidation, canonical fingerprints, response-wide resampling and
the voice TTS lifecycle. They use fake models/transports. Run from an existing
development environment with the repository requirements already available:

```bash
cd main/xiaozhi-server
python -m unittest discover -s tests -p 'test_worker_tts_segments.py'
python -m unittest discover -s tests -p 'test_worker_tts_soundbank.py'
python -m unittest discover -s tests -p 'test_worker_llm_stream.py'
python -m unittest discover -s tests -p 'test_voice_transport.py'
```

Soundbank fixtures use disposable caches and codec-only audio; they cover
ready-index/config/hash gating, private immutable copies, normalized cache hits
without RPC, optimized/canonical selection, mixed ordering, rate transitions,
first-segment protection and owned cancellation/disconnect behavior. No live
Cloud, NATS, model or robot is used.

After explicit deployment, `probe_worker_tts_segments.py` compares the same
user-provided JSON array of segments with concurrency 1 and 3. It synthesizes
only, does not play audio or print text, and uses privately supplied NATS env:

```bash
python probe_worker_tts_segments.py --node-id deskb1x \
  --bundle /private/provisioning/worker-tts.json \
  --segments-file /private/provisioning/segments.json --concurrency 1
python probe_worker_tts_segments.py --node-id deskb1x \
  --bundle /private/provisioning/worker-tts.json \
  --segments-file /private/provisioning/segments.json --concurrency 3
```

Choose benchmark text that does not match Soundbank entries: the probe rejects
recorded phrases before connecting, because cache hits do not measure native
synthesis RTF. Compare wall time divided by total generated audio duration (effective synthesis
RTF), native segment RTF, and participating worker IDs. This benchmark excludes
LLM token timing and playback. Then verify one real robot turn, ordered subtitles
and sound, abort, reconnect and node loss. Core logs report bounded synthesis
timings and playback underrun gaps without transcripts. Measure speech-end to
first audible audio and gaps on target; static review proves no RTF improvement.
