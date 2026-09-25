<p align="center">
  <img src="main/xiaozhi-server/web/settings/favicon.svg" width="96" alt="Xiaozhi Desk Robot" />
</p>

<h1 align="center">Xiaozhi Desk Robot Server</h1>

<p align="center">
  A lean, local-first Xiaozhi backend built for one Desk Robot.
</p>

<p align="center">
  <img alt="Python 3.10" src="https://img.shields.io/badge/Python-3.10-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-D3A85E?style=flat-square" />
  <img alt="Turn based" src="https://img.shields.io/badge/Conversation-Turn--based-6F5B45?style=flat-square" />
  <img alt="Docker not required" src="https://img.shields.io/badge/Docker-Not%20required-2E7D6B?style=flat-square" />
</p>

> Keep the firmware contract stable, make every turn deterministic, and stay
> small enough to understand.

This fork keeps the Xiaozhi Python runtime and provider ecosystem while removing
the upstream management, mobile, and digital-human applications. It runs
directly from source and targets a trusted, single-robot deployment.

## Status

| Area | Current state |
| --- | --- |
| Product target | One local Desk Robot |
| Conversation model | Turn-based: VAD → ASR → LLM/tools → TTS |
| Transport | Native WebSocket + HTTP; external gateway for MQTT/UDP |
| Configuration | Reference YAML + gitignored local override |
| Control plane | Local Settings UI with diagnostics and Memory editor |
| Memory | Explicit YAML Memory v2; no embeddings or vector database |
| Provider strategy | Gemini-first development, provider-neutral core |
| Validation | Provider benchmarks plus real-firmware smoke testing |

Real hardware remains the final compatibility check for audio framing, MCP,
camera, abort, reconnect, and playback behavior.

## What is included

- Xiaozhi WebSocket sessions, OTA/bootstrap, camera/vision, and audio lifecycle.
- Swappable cloud or local ASR, LLM, VLLM, TTS, memory, and intent providers.
- Dynamic device MCP, server MCP, firmware IoT tools, and server-side plugins.
- A local Settings UI for providers, configuration, resource usage, turn
  diagnostics, and memory administration.
- Explicit YAML Memory v2 with project-aware pinned context and deterministic
  lexical recall.
- Provider and plugin performance testers for latency comparisons.

```text
Desk Robot firmware
        │  Xiaozhi WebSocket / HTTP
        ▼
┌───────────────────────────────────────┐
│            xiaozhi-server             │
│ VAD → ASR → LLM ↔ MCP/tools → TTS    │
│              │                        │
│        camera / vision                │
└───────────────────────────────────────┘
        │  Opus audio + protocol events
        ▼
Desk Robot firmware
```

## Quick start

### Requirements

- Python `3.10` (`3.10.14` is pinned in `.tool-versions`)
- FFmpeg
- Opus / libopus
- Provider credentials or the required local model runtime

On macOS:

```bash
brew install ffmpeg opus
```

Install and start:

```bash
git clone https://github.com/hudrucan/xiaozhi-desk-robot-server.git
cd xiaozhi-desk-robot-server/main/xiaozhi-server

python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

mkdir -p data
touch data/.config.yaml
python app.py
```

`data/.config.yaml` is required, may initially be empty, and is gitignored. Add
provider selections, model names, endpoints, and secrets there—never in the
committed `config.yaml`.

### Default endpoints

| Service | URL |
| --- | --- |
| WebSocket | `ws://<host>:8000/xiaozhi/v1/` |
| OTA/bootstrap | `http://<host>:8003/xiaozhi/ota/` |
| Vision | `http://<host>:8003/mcp/vision/explain` |
| Settings | `http://127.0.0.1:8003/settings/` |

Settings accepts loopback requests by default. Enable
`server.settings.allow_remote` only on a trusted LAN. Configuration edits are
validated, written atomically to `data/.config.yaml`, and normally require a
restart; Memory edits apply to the next turn immediately.

## Providers

Provider selection lives under `selected_module` in YAML.

| Family | Implementations in this fork |
| --- | --- |
| VAD | Silero ONNX |
| ASR | Gemini, OpenAI-compatible, Sherpa ONNX |
| LLM | Gemini, OpenAI-compatible, llama.cpp |
| Vision | Gemini/OpenAI-compatible, llama.cpp |
| TTS | Gemini, Edge, Sherpa ONNX, VieNeu, OpenAI-compatible, custom HTTP |
| Memory | Disabled, short-summary legacy, explicit YAML v2 |
| Intent | Function calling, intent LLM, disabled |

The committed [`config.yaml`](main/xiaozhi-server/config.yaml) is the complete
reference for current options. Bundled and optional speech assets are documented
in [`models/README.md`](main/xiaozhi-server/models/README.md).

## Memory v2

Select `mem_local_explicit` to store durable records in
`data/.memory.yaml`. Memory changes only through explicit tool/UI actions; normal
conversation and disconnects do not create records or invoke a separate memory
LLM.

Recall stays local and deterministic:

- global pinned records load every turn;
- project-pinned records load for the active project;
- dynamic recall uses the current message, three recent turns, aliases, exact
  metadata matches, light typo fallback, importance, and recency;
- inactive and superseded records are excluded before ranking;
- top-K, minimum score, and character budgets bound prompt growth.

Legacy YAML entries containing only `content` are normalized automatically.

## Tools and MCP

Firmware-advertised MCP tools remain authoritative. The server also supports
external/server MCP and these built-in plugins:

- local date/time;
- Tavily or Metaso web search;
- Open-Meteo weather and air quality;
- explicit local-memory management.

Tools are enabled by configuration. Gemini can optionally use native Google
Search while other providers retain the configured `web_search` plugin.

## Local models

The repository includes the Silero VAD model plus selected Vietnamese Sherpa
ASR/TTS assets. Sherpa and VieNeu Python runtimes remain optional:

```bash
pip install -r requirements-optional.txt
```

For local text or vision inference with managed llama.cpp:

```bash
brew install llama.cpp
```

Select `LlamaCppLLM` or `LlamaCppVLLM` in the local override. Managed processes
start only when selected, reuse prompt/model state, and stop with the server.

## Measure latency

Run the benchmark for the provider selected in the merged configuration:

```bash
cd main/xiaozhi-server

python performance_tester.py asr
python performance_tester.py llm
python performance_tester.py tts
python performance_tester.py vllm

python performance_tester.py plugins web_search
python performance_tester.py plugins get_weather
python performance_tester.py plugins get_air_quality
```

Runtime Diagnostics separately keeps a bounded in-memory timeline of completed
turns, tool calls, device events, and latency stages.

## Repository map

```text
main/xiaozhi-server/
├── app.py                  # application entrypoint
├── config.yaml             # safe reference configuration
├── core/
│   ├── connection.py       # per-connection turn orchestration
│   ├── handle/             # Xiaozhi protocol handlers
│   ├── providers/          # replaceable AI/tool providers
│   └── api/                # OTA, vision, settings, memory APIs
├── plugins_func/           # server-side tools
├── web/settings/           # lightweight local control plane
├── performance_testers/    # provider/plugin benchmarks
└── models/                 # bundled and local model assets
```

Runtime state belongs under `main/xiaozhi-server/data/` and logs/generated audio
under `main/xiaozhi-server/tmp/`; both are ignored by Git.

## Scope

This project intentionally does not include `manager-api`, `manager-web`,
`manager-mobile`, or `digital-human`. It is not an enterprise management stack,
does not use Docker as the default path, and does not turn the firmware into a
continuous full-duplex assistant.

## License and origin

MIT licensed. See [`LICENSE`](LICENSE).

Derived from
[`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server).
Please preserve applicable upstream notices and review each downloaded model's
own license before redistribution.
