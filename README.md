<p align="center">
  <img src="main/xiaozhi-server/web/settings/favicon.svg" width="96" alt="Xiaozhi Desk Robot" />
</p>

<h1 align="center">Xiaozhi Desk Robot Server</h1>

<p align="center">
  A lean, local-first Xiaozhi backend built for one Desk Robot.
</p>

<p align="center">
  <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white" />
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

- Python `3.11` (`3.11.16` is pinned in `.tool-versions`)
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

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

python app.py
```

On a fresh clone, startup opens setup mode at `http://localhost:8003/setup/`.
Use the [First-run Cloud Restore Wizard](docs/first-run-cloud-restore-wizard-v1.md)
to upload the original Google Desktop OAuth JSON, authorize, select a Cloud
source/node, enter the recovery passphrase, and restore/activate before restarting.
Optionally [clone the selected backup into a new node ID](docs/clone-node-v1.md);
the wizard explains that provider credentials and secrets are copied.
SSH startup prints one tunnel command for both the UI and OAuth callback ports.
Setup runs no robot runtime services and is always localhost-only.

For a new **Local** deployment, explicitly create `data/.config.yaml` before
starting (`mkdir -p data` then `touch data/.config.yaml`) and configure your
providers. That existing Local installation bypasses the wizard.
`data/.config.yaml` may initially be empty and is gitignored. Existing
monolithic overrides still load. After migration, put provider selections, model
names, endpoints, and secrets in the corresponding `data/config.d/` files—never
in the committed reference files. Extra, unrecognized roots stay in
`data/.config.yaml`.

### Default endpoints

| Service | URL |
| --- | --- |
| WebSocket | `ws://<host>:8000/xiaozhi/v1/` |
| OTA/bootstrap | `http://<host>:8003/xiaozhi/ota/` |
| Vision | `http://<host>:8003/mcp/vision/explain` |
| Settings | `http://127.0.0.1:8003/settings/` |
| First-run setup (setup mode only) | `http://localhost:8003/setup/` |

Settings accepts loopback requests by default. Enable
`server.settings.allow_remote` only on a trusted LAN. Configuration edits are
validated and saved to the corresponding private `data/config.d/` files. A save
normally requires a restart; Memory edits apply to the next turn immediately.

## Standalone Core NATS worker

[`worker.py`](main/xiaozhi-server/worker.py) is a separate, stateless process for
queue-group RPC probes against the three-node Core NATS cluster. Normal `app.py`
does not use it or require a NATS connection. No ASR, LLM, VLM or TTS providers
are distributed yet; the worker initializes no providers, device transports,
MCP execution, HTTP/WebSocket listeners or setup UI. It uses no JetStream.

From `main/xiaozhi-server`, use Python 3.11. Production/cluster worker installs
use the minimal `requirements-worker.txt` dependency set below.
`requirements.txt` remains the full server/runtime dependency set and also
includes the same `nats-py==2.16.0` pin for normal development environments.
Supply credentials through the process environment; never place NATS passwords
in either YAML configuration file.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-worker.txt

export XIAOZHI_NATS_SERVERS='nats://10.10.10.11:4222,nats://10.10.10.12:4222,nats://10.10.10.13:4222'
export XIAOZHI_NATS_USER='xiaozhi'
# Supply XIAOZHI_NATS_PASSWORD securely in the environment before starting.
export XIAOZHI_WORKER_ID='deskb1x'  # optional
.venv/bin/python worker.py
```

The server list, username and password are required. Deployment must use the
same application/client username configured by the cluster Ansible role
(`nats_client_user`, currently `xiaozhi`). Only `nats://host[:port]`
URLs without embedded credentials, paths, queries or fragments are accepted.
Worker IDs contain 1–192 ASCII letters, digits, underscores or hyphens. The
default uses the existing hostname identity helper, replacing dots with hyphens.
Choose a unique ID for each worker so targeted requests reach one process.
Invalid configuration fails before connecting; logs omit credentials and URLs.

| Probe    | Subject                              | Queue group       |
| -------- | ------------------------------------ | ----------------- |
| Balanced | `xiaozhi.v1.worker.ping`             | `xiaozhi-workers` |
| Targeted | `xiaozhi.v1.worker.<worker_id>.ping` | None              |

Both return compact UTF-8 JSON:

```json
{
  "protocol": "xiaozhi-worker-v1",
  "worker_id": "deskb1x",
  "status": "ok",
  "capabilities": []
}
```

`capabilities` is currently always empty. Ping payloads may be empty and their
contents are ignored. Requests without a valid reply subject, with a reply
subject over 512 ASCII bytes or with payloads over 4096 bytes are dropped.
Replies are capped at 1024 bytes; request headers are not echoed. Each subscription
buffers at most 64 messages / 256 KiB, and reply publishing has a two-second limit.
All supplied servers participate in connection/failover; reconnection retries
continue indefinitely with a two-second delay. SIGTERM/SIGINT stop connection
attempts and drain subscriptions/replies before exit when connected. If draining
is unavailable (for example, during a disconnect), shutdown closes the client.

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

The committed [`config.yaml`](main/xiaozhi-server/config.yaml) lists the reference
files under [`config/defaults/`](main/xiaozhi-server/config/defaults), grouped
similarly to the Settings UI:

| Reference file | Settings |
| --- | --- |
| `runtime.yaml` | Listeners, authentication/gateways, turn limits, audio delivery, protocol hello |
| `assistant.yaml` | Prompts, wake/exit phrases, responses, behavior switches |
| `diagnostics.yaml` | Logging, turn metrics, request dumps, warning thresholds |
| `integrations.yaml` | MCP, context sources, plugins, voiceprint |
| `soundbank.yaml` | Static soundbank options and entries |
| `benchmarks.yaml` | Manual performance tester inputs |
| `providers/selection.yaml` | Active provider selections |
| `providers/{vad,asr,llm,vllm,tts,memory,intent}.yaml` | Options for each provider group |

The server and Settings editor use the same default loader. Includes are explicit,
relative to `config.yaml`, and loaded in list order; later fragments override
earlier ones. Inline settings in `config.yaml` override the fragments, and
local overrides take precedence over all reference defaults. A monolithic
`config.yaml` without `includes` remains supported. Fragments must be YAML objects,
cannot include other files, and must stay within the manifest directory.

Local section names mirror the reference table above, including
`data/config.d/providers/tts.yaml` and `data/config.d/providers/selection.yaml`.
The loader merges any remaining `data/.config.yaml` values first, then local
sections in the table's order; section values win on conflicts. Keep each setting
in its owning section. Section files cannot include other files, and misplaced
settings or unknown `.yaml` section files are rejected rather than ignored.

Reads of an old monolithic override do not migrate it. The first Settings save
splits its values into local sections. To migrate explicitly without starting the
server, stop the old server and run from the repository root using the server's
Python environment:

```bash
python scripts/split_local_config.py
```

Migration preserves every override, including secrets and unknown roots. The
original YAML bytes are kept in `data/.config.yaml.pre-split.backup`; subsequent
saves keep the previous complete override in `data/.config.yaml.backup`. Restore
by stopping the server, moving `data/config.d/` aside, and copying the desired
backup to `data/.config.yaml`.
If a transaction is still pending, move its `.config.transaction.yaml` journal and
matching `.config-stage-*` directory aside with the section files before restoring.

Settings retains secret masking, validation, and restart behavior. All config
readers and writers share a lock. Changed files are staged and synced before a
transaction journal is published; a reader completes any interrupted committed
write before loading config. A failed save after journal publication can therefore
be applied by the next reader. Hand edits should be made with the server stopped.
Restart the server with the new code before using Settings after migration.

The focused persistence checks (including interrupted saves) can be run from
`main/xiaozhi-server` with `python -m unittest discover -s tests -p test_local_config.py`.
Memory records remain in `data/.memory.yaml`, separately from provider settings.
Bundled and optional speech assets are documented in
[`models/README.md`](main/xiaozhi-server/models/README.md).

Optional centralized configuration is available through private Google Drive.
Local remains the default when `data/bootstrap.yaml` is absent. See
[Cloud Config V1](docs/cloud-config-v1.md) for provisioning, explicit source
switching, desired/active revisions and offline last-known-good behavior.

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
├── config.yaml             # reference configuration manifest
├── config/defaults/        # safe defaults grouped by subsystem/provider
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
