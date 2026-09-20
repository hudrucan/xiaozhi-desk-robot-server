# AGENTS.md

## Mission

Work on this repository as a **lean, local Xiaozhi server for one Desk Robot**.

The main objective is to preserve the existing Xiaozhi firmware behavior while improving:

1. stability,
2. deterministic turn handling,
3. MCP/tool reliability,
4. provider flexibility,
5. per-turn latency,
6. maintainability.

Do not redesign the robot into a continuous realtime/full-duplex assistant unless the user explicitly changes the product requirement.

## Repository scope

The product code is:

```text
main/xiaozhi-server/
```

The following upstream applications were intentionally removed and must not be restored unless explicitly requested:

```text
main/manager-api/
main/manager-web/
main/manager-mobile/
main/digital-human/
```

The repository is intentionally source-first and lightweight.

**Do not introduce Docker as the default development or deployment path.**

## Source of truth

Before changing behavior, inspect the current code.

Important locations:

```text
main/xiaozhi-server/app.py
main/xiaozhi-server/config.yaml
main/xiaozhi-server/config/
main/xiaozhi-server/core/connection.py
main/xiaozhi-server/core/websocket_server.py
main/xiaozhi-server/core/http_server.py
main/xiaozhi-server/core/handle/
main/xiaozhi-server/core/providers/
main/xiaozhi-server/core/api/
main/xiaozhi-server/core/utils/
main/xiaozhi-server/plugins_func/
main/xiaozhi-server/performance_tester.py
```

Do not assume old upstream documentation still describes this trimmed fork accurately.

The core currently contains some inherited references to removed upstream components. Treat them as legacy residue until code tracing proves otherwise.

## Firmware compatibility

The target is the current Desk Robot Xiaozhi firmware.

Preserve the existing Xiaozhi contract and user-visible behavior, including where applicable:

- bootstrap / OTA behavior,
- session/hello behavior,
- audio framing,
- listen/start/stop/detect semantics,
- abort behavior,
- STT/TTS lifecycle,
- MCP discovery and tool calling,
- camera/vision flow,
- emotion/display messages,
- reconnect and cleanup semantics.

Do not require firmware changes merely to make a server refactor easier.

When compatibility is uncertain, trace the server code and the relevant firmware protocol code before changing the contract.

## Turn model

The Desk Robot is **turn-based by design**.

A typical turn is:

```text
listen
→ audio
→ VAD
→ ASR
→ LLM
→ zero or more MCP/tool calls
→ final response
→ TTS
→ playback
→ done
```

Many robot behaviors, games, display states, emotions, movements, camera actions, and MCP tools depend on deterministic turn ownership.

Realtime/native-audio backends may be added as implementation options, but they must adapt to Xiaozhi turn semantics rather than replacing them.

## Provider architecture

Keep provider boundaries intact.

ASR, LLM, TTS, VLLM, memory, intent, and future native-audio backends should remain replaceable without changing the protocol core.

Do not delete the provider ecosystem just to make the repository smaller.

Prefer:

- disabling unused providers,
- isolating provider-specific dependencies,
- lazy initialization where safe,
- cleaner interfaces,
- targeted removal only when a provider is intentionally unsupported.

Current product priority is **Gemini first**, but the architecture must not become Gemini-coupled.

Gemini Live is optional and must not dictate the server lifecycle.

## Configuration rules

The committed `config.yaml` is the default/reference configuration.

Local overrides belong in:

```text
main/xiaozhi-server/data/.config.yaml
```

Never commit:

- API keys,
- access tokens,
- private endpoints,
- device secrets,
- local credentials.

Do not hard-code secrets in provider implementations.

When adding configuration:

1. add a safe default,
2. document the key,
3. preserve backward compatibility when practical,
4. keep provider-specific settings inside the provider's config section.

Do not implement locale selection with hard-coded language branches in Python.
User-facing text that varies by deployment or language belongs in configuration,
with an English value as the committed default. Put local-language overrides in
`main/xiaozhi-server/data/.config.yaml`.

## Transport

The core Python runtime directly runs WebSocket and HTTP services.

MQTT + UDP deployments use the external Xiaozhi MQTT gateway; it is not part of this repository.

Do not silently replace one transport with another.

Protocol/session logic should be kept as transport-independent as practical, but compatibility is more important than abstraction purity.

## MCP and tools

Device MCP is a first-class requirement.

Do not hard-code Desk Robot tools on the server when the firmware already advertises them dynamically.

Preserve:

- tool schemas,
- tool names,
- request/result correlation,
- multiple tool calls,
- errors,
- timeouts,
- turn ownership.

Do not optimize away the second LLM step after a tool call unless the tool contract explicitly guarantees that its result is already the final user-facing response.

Camera behavior should remain part of the tool/turn flow, not become an unrelated realtime camera stream.

## Latency work

Optimize **per-turn latency**, not continuous conversation.

Measure before changing architecture.

Key timings:

```text
speech end → ASR
ASR → first LLM token/tool call
tool call → tool result
tool result → resumed LLM output
response text → first TTS audio
speech end → first audible response
full turn duration
```

Prefer removing unnecessary waits, batching, repeated initialization, and serial work before introducing architectural complexity.

Use `performance_tester.py` where applicable.

Do not claim a latency improvement without measurements.

## Stability and concurrency

Prioritize deterministic cleanup over clever concurrency.

For every async task, queue, worker, callback, or provider stream, establish:

- owner,
- lifetime,
- cancellation path,
- disconnect behavior,
- stale-result behavior,
- timeout behavior.

Avoid orphan tasks and callbacks from an old turn/session mutating current state.

Abort/disconnect paths must be idempotent where possible.

## Refactoring rules

Fix the proven root cause with the smallest targeted change. Minimize changes to
the core runtime; prefer a provider-local or configuration change when it can
solve the issue without altering shared protocol or session behavior. Do not add
new modes, abstractions, fallbacks, or adjacent cleanup unless the task requires
them.

Do not perform broad rewrites before establishing a working baseline.

Prefer small phases that can be tested independently.

When touching `core/connection.py` or cross-cutting session code:

1. identify the current behavior,
2. identify all callers,
3. preserve protocol behavior,
4. document a concise manual smoke-test procedure when useful,
5. change one responsibility at a time.

A large file is not, by itself, justification for a rewrite.

## Removed upstream management stack

Do not build new code around:

- manager-api,
- manager-web,
- manager-mobile,
- digital-human.

If inherited core code still mentions them, determine whether the path is actually reachable in the current local configuration.

Examples of expected stale residue may include:

- `read_config_from_api`,
- manager API client helpers,
- log messages referencing digital-human,
- `.gitignore` rules for removed folders.

Clean these incrementally after confirming they are not required.

## Local development

Preferred environment:

- Python 3.10
- FFmpeg installed
- Opus/libopus installed
- no Docker required

Typical setup:

```bash
cd main/xiaozhi-server

python3.10 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

mkdir -p data
touch data/.config.yaml

python app.py
```

A Conda Python 3.10 environment is acceptable when native/ML dependencies are easier to install that way.

## Validation

By default, validation in agent work is limited to static logic review:

- trace callers and affected control flow,
- inspect imports and configuration boundaries,
- search for relevant references and stale paths,
- inspect the final diff for scope and consistency.

Do not run tests, compilation, builds, package installation, server startup, or
runtime smoke tests unless the user explicitly requests them. The user owns
runtime validation.

Do not add or modify test files by default. Add only the smallest necessary test
when the change cannot be reviewed safely without one or when the user explicitly
requests tests. Test names, fixtures, comments, and test data must be written in
English, not Vietnamese.

When handing off a meaningful server change, suggest the smallest applicable
manual checks for the user to run:

- server starts without tracebacks,
- local config loads,
- WebSocket listener starts,
- HTTP listener starts,
- OTA/bootstrap endpoint responds,
- device hello succeeds,
- voice input reaches ASR,
- LLM returns,
- MCP discovery/call works when relevant,
- TTS audio is returned,
- abort works,
- reconnect creates clean state,
- camera/vision works when touched.

For provider-only changes, suggest a direct provider check plus one end-to-end
turn when practical. For session/protocol changes, explicitly note that real
firmware validation remains required.

## Current non-goals

Unless explicitly requested, do not spend time on:

- rebuilding an enterprise management platform,
- multi-user account management,
- mobile management apps,
- digital-human/avatar clients,
- continuous realtime/full-duplex conversion,
- Docker orchestration,
- rewriting the firmware,
- replacing the Xiaozhi protocol,
- removing working provider integrations solely for cosmetic cleanup.

## Near-term direction

The preferred order is:

1. make the current trimmed server run locally,
2. establish an end-to-end Desk Robot baseline,
3. measure latency and failures,
4. stabilize the existing turn path,
5. optimize Gemini-first configuration,
6. compare ASR/TTS alternatives,
7. remove proven-unused upstream residue,
8. add lightweight direct-text / settings features later,
9. evaluate native audio only if measurements justify it.

## Change discipline

Before making a destructive change:

- explain what depends on it,
- identify the rollback,
- avoid mixing cleanup, refactor, and new features in one large patch.

Do not delete code because it "looks unused" without tracing imports/config/references.

Do not reintroduce deleted upstream components to satisfy a stale reference; fix or remove the stale reference if it is truly dead.

Keep commits narrow and descriptive.
