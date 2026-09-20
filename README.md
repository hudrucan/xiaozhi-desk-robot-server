# Xiaozhi Desk Robot Server

A lean, self-hosted Xiaozhi backend for the Desk Robot.

This repository is derived from `xinnan-tech/xiaozhi-esp32-server`, but intentionally keeps only the core Python runtime needed for the robot. The management backend, web/mobile management apps, and digital-human test app have been removed.

The current goal is simple:

> Preserve Xiaozhi firmware behavior, keep the provider ecosystem, make the local server stable and easy to customize, and reduce per-turn latency as much as possible.

## Scope

```text
xiaozhi-desk-robot-server/
├─ main/
│  └─ xiaozhi-server/   # core runtime
├─ .gitignore
└─ LICENSE
```

The core runtime provides:

- Xiaozhi WebSocket protocol support
- HTTP bootstrap / OTA endpoints
- camera / vision endpoint
- VAD
- interchangeable ASR providers
- interchangeable LLM providers
- interchangeable TTS providers
- VLLM / vision providers
- MCP and tool calling
- server-side plugins
- memory / intent modules
- provider performance testing

This fork is intended for a local, trusted Desk Robot deployment rather than a multi-user management platform.

## Architecture

```text
Xiaozhi firmware
      │
      │ Xiaozhi protocol
      ▼
┌──────────────────────────────┐
│       xiaozhi-server         │
│                              │
│  session / turn lifecycle    │
│      │                       │
│      ├─ VAD                  │
│      ├─ ASR                  │
│      ├─ LLM                  │
│      ├─ MCP / tools          │
│      ├─ vision / camera      │
│      └─ TTS                  │
└──────────────────────────────┘
      │
      ▼
Xiaozhi firmware
```

The robot remains **turn-based**. Realtime/native-audio providers may be evaluated later, but they must not take ownership of the firmware lifecycle or break deterministic MCP/tool behavior.

## Repository layout

The important directory is:

```text
main/xiaozhi-server/
├─ app.py
├─ config.yaml
├─ requirements.txt
├─ performance_tester.py
├─ config/
├─ core/
│  ├─ api/
│  ├─ handle/
│  ├─ providers/
│  ├─ utils/
│  ├─ connection.py
│  ├─ http_server.py
│  └─ websocket_server.py
├─ plugins_func/
└─ data/                 # local runtime config/data; gitignored
```

Key entry points:

- `app.py` — starts the core runtime.
- `core/websocket_server.py` — Xiaozhi WebSocket server and session creation.
- `core/http_server.py` — local HTTP server for OTA/bootstrap and vision.
- `core/connection.py` — per-device session/orchestration.
- `core/handle/` — protocol and turn handlers.
- `core/providers/` — VAD, ASR, LLM, TTS, memory, intent, and vision providers.
- `plugins_func/` — server-side function/tool plugins.
- `performance_tester.py` — provider latency/response testing.

## Transport

The standalone Python runtime directly exposes:

- WebSocket: default port `8000`
- HTTP: default port `8003`

The HTTP server includes the lightweight local endpoints used for:

- `/xiaozhi/ota/`
- `/xiaozhi/ota/download/{filename}`
- `/mcp/vision/explain`

MQTT + UDP remains a supported Xiaozhi deployment path through the external Xiaozhi MQTT gateway. The gateway is **not bundled in this repository**.

## Local installation

This project is intended to run directly from source. Docker is not required.

### Requirements

- Python 3.10
- FFmpeg
- Opus / libopus
- network access for any configured cloud providers

On macOS:

```bash
brew install ffmpeg opus
```

Create a Python 3.10 environment, then:

```bash
cd main/xiaozhi-server

python3.10 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

If a dependency does not install cleanly in a plain virtual environment, use a Python 3.10 Conda environment instead. The upstream dependency set includes native/audio/ML packages.

## Configuration

`data/.config.yaml` is required by the current runtime and is intended for machine-local overrides.

```bash
cd main/xiaozhi-server
mkdir -p data
touch data/.config.yaml
```

Do **not** put API keys directly into committed files.

Configuration is merged with `config.yaml`, so the local file only needs the values you want to override.

Typical selections:

```yaml
selected_module:
  VAD: SileroVAD
  ASR: FunASR
  LLM: ChatGLMLLM
  VLLM: ChatGLMVLLM
  TTS: EdgeTTS
  Memory: nomem
  Intent: function_call
```

Override those names with the providers you actually want to use.

The default local ASR is FunASR and expects its configured model files to exist. Cloud ASR can be selected instead.

## Run

```bash
cd main/xiaozhi-server
source .venv/bin/activate
python app.py
```

Default local endpoints:

```text
WebSocket  ws://<host>:8000/xiaozhi/v1/
HTTP       http://<host>:8003/
OTA        http://<host>:8003/xiaozhi/ota/
Vision     http://<host>:8003/mcp/vision/explain
```

Point the firmware OTA/bootstrap URL at the local HTTP endpoint when testing the standalone server.

## Provider strategy

The provider architecture is a feature, not bloat.

The project should remain able to swap and benchmark:

- ASR independently
- LLM independently
- TTS independently
- vision models independently

Current development priority is **Gemini first** where it provides the best cost/quality tradeoff, while keeping the architecture provider-neutral.

Gemini Live/native audio is optional. It is not a requirement for the core architecture.

## Latency

The primary latency target is **time per Xiaozhi turn**, not continuous full-duplex conversation.

Optimize the critical path:

```text
end of speech
→ ASR
→ LLM first decision
→ optional MCP/tool calls
→ LLM continuation
→ TTS first audio
→ playback
```

Useful areas to measure:

- end-of-speech → ASR result
- ASR result → first LLM token/tool call
- MCP call duration
- tool result → first response token
- response text → first TTS audio
- end-to-end turn latency

Use:

```bash
cd main/xiaozhi-server
python performance_tester.py

# Or run one active provider directly.
python performance_tester.py asr  # or llm, tts, vllm
```

Each benchmark runs the provider selected in the merged configuration. Set
`PERF_RUNS`, `PERF_TIMEOUT_SECONDS`, `PERF_ASR_AUDIO`, `PERF_LLM_PROMPT`, or
`PERF_TTS_TEXT` to override its small default workload.

The LLM benchmark samples prompts from `module_test.test_sentences` using a
reproducible shuffle. Set `PERF_LLM_SEED` for a different order or
`PERF_LLM_PROMPT` for one fixed prompt. The selected provider and model always
come from the merged server configuration.

## MCP and Desk Robot behavior

Device-side MCP is a core requirement for this fork.

The server must preserve deterministic turn behavior for robot features such as:

- movement
- camera
- sensors
- display / emotion state
- games and turn-based interactions
- other firmware-advertised tools

Do not replace device MCP with server-specific hard-coded robot behavior unless there is a clear reason to do so.

## Current development priorities

1. Run the trimmed core server locally without Docker.
2. Verify end-to-end compatibility with the Desk Robot firmware.
3. Stabilize voice → ASR → LLM → MCP → TTS turns.
4. Measure latency before refactoring.
5. Configure and optimize Gemini-first provider paths.
6. Benchmark alternative ASR and TTS providers.
7. Clean stale upstream management references from the core only when they are proven unused.
8. Add a small Desk Robot-specific settings UI later if useful.

Future work may include direct raw-text input and native-audio providers, but those are not required for the initial stable server.

## Removed upstream applications

The following upstream applications were intentionally removed:

- `manager-api`
- `manager-web`
- `manager-mobile`
- `digital-human`

Do not restore them as dependencies of the Desk Robot runtime.

Some legacy references to these applications may still exist inside inherited core code or ignore rules. They are cleanup targets, not evidence that the removed applications are required.

## License

MIT. See [LICENSE](LICENSE).

This repository retains code derived from `xinnan-tech/xiaozhi-esp32-server`; preserve the applicable copyright and license notices.
