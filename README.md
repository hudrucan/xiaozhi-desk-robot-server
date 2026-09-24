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
├─ performance_testers/
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
├─ web/
│  └─ settings/           # lightweight local settings UI
└─ data/                 # local runtime config/data; gitignored
```

Key entry points:

- `app.py` — starts the core runtime.
- `core/websocket_server.py` — Xiaozhi WebSocket server and session creation.
- `core/http_server.py` — local HTTP server for OTA/bootstrap, vision, and settings.
- `core/connection.py` — per-device session/orchestration.
- `core/handle/` — protocol and turn handlers.
- `core/providers/` — VAD, ASR, LLM, TTS, memory, intent, and vision providers.
- `plugins_func/` — server-side function/tool plugins.
- `performance_tester.py` — provider latency/response testing.
- `performance_testers/` — provider and grouped plugin benchmark implementations.

## Transport

The standalone Python runtime directly exposes:

- WebSocket: default port `8000`
- HTTP: default port `8003`

The HTTP server includes the lightweight local endpoints used for:

- `/xiaozhi/ota/`
- `/xiaozhi/ota/download/{filename}`
- `/mcp/vision/explain`
- `/settings/` (local requests only by default)

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
  ASR: OpenAIASR
  LLM: OpenAILLM
  VLLM: OpenAIVLLM
  TTS: EdgeTTS
  Memory: nomem
  Intent: function_call
```

These are the safe reference selections in committed `config.yaml`. Their API
keys, model names, voices, and endpoints are placeholders; override them in
`data/.config.yaml` before starting the server. Local Sherpa ASR/TTS providers
are also available when their optional runtime and model files are installed.

For local vision, select `LlamaCppVLLM`. Its managed Qwen2.5-VL process uses a
separate port from the text LLM, starts lazily on the first camera request, is
reused by later image requests, and stops with the HTTP server. The first image
request therefore includes model loading time. Override `response_language` in
`data/.config.yaml` for the deployment language. If the selected local LLM uses
the same model source and a compatible context/projector configuration, vision
reuses that live llama.cpp endpoint instead of loading a second model process.

For opt-in local memory, select `mem_local_explicit`. It stores compact facts in
`data/.memory.yaml` only when the user explicitly asks the assistant to remember
or forget something. It does not summarize conversations on disconnect or call
an additional memory LLM. With `recall_enabled: true`, a bounded lexical lookup
automatically supplies only facts relevant to the current request. Set it to
`false` to disable recall while keeping explicitly saved facts on disk.

### Search and weather tools

The built-in `get_current_datetime` tool reads the server's local clock and is
enabled by default. It does not require provider configuration or an API key.

Server plugins are disabled until their provider-specific configuration is set
in `data/.config.yaml`. A typical Vietnamese deployment can use:

```yaml
plugins:
  web_search:
    provider: tavily
    api_key: your_tavily_api_key
    max_results: 5
    search_depth: advanced
    include_answer: advanced
    country: vietnam
  get_weather:
    provider: open_meteo
    default_location: Ho Chi Minh City
    location_aliases:
      TP.HCM: Ho Chi Minh City
      TPHCM: Ho Chi Minh City
    language: vi
    preferred_country_code: VN
    forecast_days: 7
    cache_ttl_seconds: 1800
  get_air_quality:
    provider: open_meteo
    default_location: Ho Chi Minh City
    language: vi
    preferred_country_code: VN
    forecast_hours: 24
    cache_ttl_seconds: 1800
```

Open-Meteo does not require an API key. `web_search` supports Tavily and Metaso;
only configure options accepted by the selected provider. Weather location
aliases are exact and case-insensitive; use them for local abbreviations that
Open-Meteo's geocoder does not resolve reliably.

Set the top-level `tool_error_response` and `tool_timeout_response` values in
`data/.config.yaml` to keep spoken tool failures in the deployment language.
Detailed provider and device errors remain in the server log.

Gemini 3 models can use Google's native search grounding instead of the
`web_search` plugin:

```yaml
LLM:
  GeminiLLM:
    native_google_search: true
```

Native Google Search applies only to Gemini. Keep `plugins.web_search`
configured when other LLM providers need a search tool; Gemini excludes the
custom `web_search` tool while native grounding is enabled.

### Local LLM with llama.cpp

The dedicated llama.cpp provider runs a local OpenAI-compatible server,
including streaming and function calls. Install the lightweight runtime:

```bash
brew install llama.cpp
```

Then select it in `data/.config.yaml`:

```yaml
selected_module:
  LLM: LlamaCppLLM

LLM:
  LlamaCppLLM:
    type: llama_cpp
    api_key: local
    model_name: qwen3:4b
    temperature: 0.6
    top_p: 0.95
    max_history_messages: 8
    process:
      managed: true
      executable: llama-server
      hf_model: Qwen/Qwen3-4B-GGUF:Q4_K_M
      host: 127.0.0.1
      port: 6000
      context_size: 8192
      gpu_layers: all
      parallel: 1
      cache_reuse: 64
      reasoning: "off"
      startup_timeout: 900
      shutdown_timeout: 10
      log_file: tmp/llama-server.log
      sleep_idle_seconds: 600
```

With `managed: true`, `llama-server` starts only when this provider is selected
and stops during normal application shutdown or configuration restart. Set
`managed: false` and configure `base_url` to connect to an externally managed
llama.cpp endpoint instead. For a managed server, `base_url` is derived from
`process.host` and `process.port`.
After `sleep_idle_seconds` expires, the next inference request reloads the model
and rebuilds the prompt cache, so that first response has cold-start latency.
The bounded history and cache reuse settings keep the repeated MCP/tool schemas
from forcing a full prompt prefill on every conversational turn.
For the single-device deployment, `config/device_mcp_tools.json` seeds the known
firmware tool inventory. llama.cpp prewarms that stable prefix before opening the
WebSocket listener. The live `tools/list` response is still authoritative: an
order-independent schema fingerprint keeps the warm cache when it matches and
writes changed schemas to `data/.device_mcp_tools.json` for the next startup.
This cache is inactive for cloud LLM providers; they continue to use only the
inventory reported by the connected firmware.
Use `model_path` instead of `hf_model` to avoid network access and load an
existing GGUF file. Keep the `web_search` plugin configured if this provider
should be able to search the web; native Google Search grounding remains
Gemini-only.

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
Settings   http://127.0.0.1:8003/settings/
```

Point the firmware OTA/bootstrap URL at the local HTTP endpoint when testing the standalone server.
The Settings UI writes local overrides to `data/.config.yaml` and requires a
restart after changes. It accepts loopback requests only unless
`server.settings.allow_remote` is explicitly enabled.

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

# Benchmark a server plugin without running the LLM.
python performance_tester.py plugins web_search
python performance_tester.py plugins get_weather
python performance_tester.py plugins get_air_quality
```

ASR, LLM, TTS, and VLLM benchmarks run the provider selected in the merged
configuration. Plugin benchmarks call the provider configured under `plugins`
directly, so they measure the external lookup without LLM continuation or the
plugin result cache. Set
`PERF_RUNS`, `PERF_TIMEOUT_SECONDS`, `PERF_ASR_AUDIO`, `PERF_LLM_PROMPT`, or
`PERF_TTS_TEXT` to override its small default workload.

The LLM benchmark samples prompts from `module_test.test_sentences` using a
reproducible shuffle. Set `PERF_LLM_SEED` for a different order or
`PERF_LLM_PROMPT` for one fixed prompt. The selected provider and model always
come from the merged server configuration. When llama.cpp prewarming is enabled,
the benchmark reports prewarm time separately, then sends the same cached device
and server-plugin tool schemas during each measured sample.

For plugin benchmarks, use `PERF_WEB_SEARCH_QUERY`, `PERF_WEATHER_LOCATION`, or
`PERF_AIR_QUALITY_LOCATION` to run one fixed input. Result logging defaults to a
500-character preview; set the corresponding `*_PREVIEW_CHARS=0` variable for
the complete provider response. The corresponding `*_SEED` variables control
reproducible sampling from the query or location lists under `module_test`.

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
