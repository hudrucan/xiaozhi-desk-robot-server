# Technical Documentation: `xiaozhi-desk-robot-server`

## Table of Contents

1. [Introduction](#1-introduction)
2. [Overall Architecture](#2-overall-architecture)
3. [`xiaozhi-server` Core Runtime](#3-xiaozhi-server-core-runtime)
4. [Data Flow and Interaction](#4-data-flow-and-interaction)
5. [Key Features](#5-key-features)
6. [Deployment and Configuration](#6-deployment-and-configuration)
7. [Project Scope](#7-project-scope)

---

## 1. Introduction

`xiaozhi-desk-robot-server` is a lightweight, self-hosted Xiaozhi backend focused on the core AI runtime used by the Desk Robot.

The project keeps the `xiaozhi-server` runtime and its provider ecosystem while removing the management and test applications that are not required for a local, single-robot deployment.

The server is responsible for:

- Receiving audio and control messages from Xiaozhi-compatible firmware.
- Running Voice Activity Detection (VAD).
- Running Automatic Speech Recognition (ASR).
- Sending recognized text to a configurable Large Language Model (LLM).
- Exposing tools and MCP capabilities to the model.
- Handling camera / vision requests when enabled.
- Synthesizing responses through a configurable Text-to-Speech (TTS) provider.
- Streaming response audio and Xiaozhi lifecycle events back to the device.
- Loading runtime configuration from local YAML files.

The design goal is to keep the Xiaozhi protocol and provider flexibility while making the server easier to run, understand, customize, and optimize for one Desk Robot.

---

## 2. Overall Architecture

The current repository is intentionally centered on a single runtime component:

```text
xiaozhi-desk-robot-server
└─ main/
   └─ xiaozhi-server/
      ├─ protocol / connection handling
      ├─ VAD
      ├─ ASR providers
      ├─ LLM providers
      ├─ TTS providers
      ├─ MCP / tools
      ├─ vision / camera support
      ├─ HTTP / OTA support
      └─ local configuration
```

High-level runtime flow:

```text
ESP32 firmware
    │
    │ Xiaozhi protocol
    ▼
xiaozhi-server
    │
    ├─ VAD
    ├─ ASR
    ├─ LLM
    ├─ MCP / tools
    ├─ vision
    └─ TTS
    │
    ▼
ESP32 firmware
```

The AI providers are pluggable. The server can use local or cloud services depending on configuration.

The core runtime should remain independent from any specific ASR, LLM, TTS, or native-audio provider.

---

## 3. `xiaozhi-server` Core Runtime

`main/xiaozhi-server` is the application that matters for the Desk Robot.

### 3.1. Provider architecture

AI services are organized under `core/providers/`.

Provider families include:

- VAD
- ASR
- LLM
- TTS
- Memory
- Intent
- Vision / VLLM

The configured implementation for each family is selected through YAML configuration.

`core/utils/modules_initialize.py` is responsible for loading and initializing the configured providers.

This provider model makes it possible to:

- Switch cloud providers without modifying firmware.
- Test local and cloud ASR implementations.
- Replace the LLM independently of ASR or TTS.
- Benchmark different ASR / LLM / TTS combinations.
- Add new provider implementations without changing the Xiaozhi protocol layer.

### 3.2. Connection and protocol handling

The main runtime uses:

- `core/websocket_server.py`
- `core/connection.py`
- `core/handle/`

Each connected device receives its own connection/session state.

The handler modules process the main Xiaozhi message and audio lifecycle, including:

- hello / session setup
- audio input
- listen state
- abort
- ASR results
- LLM responses
- MCP / tool calls
- TTS output
- reports and runtime events

The server should preserve Xiaozhi turn semantics so that firmware behavior remains deterministic.

### 3.3. MCP and tools

The runtime supports tool execution through the Xiaozhi tool stack.

Tool sources may include:

- Device-side MCP tools exposed by the ESP32 firmware.
- Server-side plugins.
- Optional external integrations configured in the runtime.

For the Desk Robot, device MCP is especially important because robot capabilities such as movement, camera actions, display behavior, sensors, and other hardware features can be exposed as tools without hard-coding them into the server.

### 3.4. Vision / camera

Vision support is part of the core runtime.

Camera-related requests can be handled through the server's HTTP / vision path and then passed to the configured vision-capable model or provider.

This allows the firmware to keep its current camera tool flow while the server decides which backend performs image understanding.

### 3.5. HTTP / OTA support

`core/http_server.py` provides auxiliary HTTP endpoints used by the Xiaozhi runtime.

Typical responsibilities include:

- OTA / bootstrap responses.
- Firmware download support.
- Vision / camera upload endpoints.
- Other lightweight runtime HTTP functions.

These endpoints are part of the core server and are independent from any removed management application.

---

## 4. Data Flow and Interaction

### 4.1. Voice turn

A normal turn follows the Xiaozhi interaction model:

```text
listen
  ↓
audio input
  ↓
VAD
  ↓
ASR
  ↓
recognized text
  ↓
LLM
  ↓
optional MCP / tool calls
  ↓
final response text
  ↓
TTS
  ↓
tts:start
  ↓
audio stream
  ↓
tts:stop
```

The goal is not to convert the robot into a continuous full-duplex realtime assistant.

The goal is to keep the existing turn-based firmware behavior while minimizing latency inside each turn.

### 4.2. Tool-assisted turn

For turns that require robot capabilities:

```text
ASR text
  ↓
LLM
  ↓
tool call
  ↓
device MCP / server tool
  ↓
tool result
  ↓
LLM continuation
  ↓
TTS response
```

The turn remains owned by the Xiaozhi session until it completes or is aborted.

### 4.3. Abort and interruption

The firmware can interrupt a response through the Xiaozhi abort flow.

The server must stop or discard stale work associated with the interrupted response and return the session to a valid state.

Correct cancellation and cleanup are important for stability.

---

## 5. Key Features

### Multi-provider AI stack

The core runtime supports interchangeable providers for:

- VAD
- ASR
- LLM
- TTS
- vision
- memory
- intent

This is intentionally preserved because provider flexibility is useful for comparing cost, latency, quality, and Vietnamese-language performance.

### Device MCP

Device-side MCP allows firmware capabilities to be discovered and invoked dynamically by the server / LLM.

This is a central capability for the Desk Robot.

### Plugin system

The server includes a plugin mechanism for adding server-side functions and external integrations.

Plugins can be enabled or removed depending on the deployment.

### Streaming-oriented processing

Where supported by the selected provider, the runtime can process and return data incrementally to reduce turn latency.

Streaming should improve response time without changing the firmware's turn-based behavior.

### Local configuration

The lightweight runtime can be configured entirely through local YAML files.

This is the preferred configuration mode for this project.

---

## 6. Deployment and Configuration

This fork is intended to run directly from source.

### 6.1. Core runtime

The main application is:

```text
main/xiaozhi-server/
```

Typical runtime dependencies include:

- Python
- Opus
- FFmpeg
- Python packages from `requirements.txt`

Run from the core directory:

```bash
python app.py
```

### 6.2. Configuration files

The runtime uses the main configuration and an optional local override.

Typical layout:

```text
main/xiaozhi-server/
├─ config.yaml
└─ data/
   └─ .config.yaml
```

`config.yaml` contains the available/default configuration.

`data/.config.yaml` can be used for local overrides such as:

- selected providers
- model names
- API keys
- endpoints
- prompts
- VAD parameters
- TTS voices
- runtime ports

Keeping local overrides separate makes it easier to update the core configuration without losing machine-specific settings.

### 6.3. Provider selection

A typical configuration selects one implementation from each provider family:

```yaml
selected_module:
  VAD: SileroVAD
  ASR: <provider>
  LLM: <provider>
  TTS: <provider>
  Memory: nomem
  Intent: function_call
```

The exact provider configuration is defined in the corresponding provider section of the YAML file.

### 6.4. Runtime priorities for the Desk Robot

The current optimization priorities are:

1. Protocol and firmware compatibility.
2. Stable turn lifecycle.
3. MCP and camera reliability.
4. Low ASR latency.
5. Low LLM first-token / first-tool latency.
6. Low TTS first-audio latency.
7. Clean cancellation and recovery.
8. Provider flexibility.

Gemini is currently a high-priority cloud provider to evaluate because of cost and availability, but the architecture should not depend on Gemini specifically.

---

## 7. Project Scope

This repository intentionally excludes the previously bundled management and test applications.

The project scope is now:

```text
KEEP
└─ main/xiaozhi-server/
   ├─ Xiaozhi protocol runtime
   ├─ AI providers
   ├─ MCP / tools
   ├─ camera / vision
   ├─ HTTP / OTA
   └─ local configuration
```

The server is intended for:

- a local Desk Robot deployment
- a small number of trusted devices
- direct source-based development
- custom firmware integration
- provider experimentation
- latency optimization
- MCP-heavy robot behavior

Configuration and future control interfaces should remain lightweight.

If a Web UI is added later, it should be a small Desk Robot-specific control plane built around the core server rather than a separate enterprise management stack.

---

## License

See the repository `LICENSE` file.
