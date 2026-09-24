const SUMMARY_GROUPS = ["VAD", "ASR", "LLM", "VLLM", "TTS"];
const PROVIDER_GROUPS = [...SUMMARY_GROUPS, "Memory", "Intent"];
const SECRET_NAMES = new Set([
  "access_key", "access_key_secret", "access_token", "api_key", "auth_key",
  "authorization", "client_secret", "mqtt_signature_key", "password",
  "mcp_endpoint", "personal_access_token", "private_key", "secret", "secret_key",
  "token",
]);

const state = {
  config: {},
  original: {},
  patch: {},
  configuredSecrets: new Set(),
  restartRequired: false,
  resources: null,
};

const $ = (selector) => document.querySelector(selector);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function getPath(object, path, fallback = undefined) {
  const value = path.split(".").reduce((current, key) => current?.[key], object);
  return value === undefined ? fallback : value;
}

function setPath(object, path, value) {
  const parts = path.split(".");
  let target = object;
  parts.slice(0, -1).forEach((part) => {
    if (!target[part] || typeof target[part] !== "object" || Array.isArray(target[part])) {
      target[part] = {};
    }
    target = target[part];
  });
  target[parts.at(-1)] = value;
}

function updateValue(path, value) {
  setPath(state.config, path, value);
  setPath(state.patch, path, value);
  updateDirtyState();
  renderOverview();
}

function isSecret(path) {
  const key = path.split(".").at(-1).toLowerCase();
  return SECRET_NAMES.has(key) || key.endsWith("_token") || key.endsWith("_secret");
}

function labelFor(key) {
  return key
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function pathHint(path) {
  return `<small>${escapeHtml(path)}</small>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function field(path, label, options = {}) {
  const value = getPath(state.config, path, options.defaultValue ?? "");
  const wide = options.wide ? " wide" : "";
  const help = options.help ? `<p class="field-help">${escapeHtml(options.help)}</p>` : "";
  const id = `field-${path.replaceAll(".", "-")}`;

  if (options.type === "boolean" || typeof value === "boolean") {
    return `
      <article class="field-card toggle-card">
        <div class="toggle-row">
          <div class="toggle-copy">
            <div class="field-label"><span>${escapeHtml(label)}</span> ${pathHint(path)}</div>
            ${help}
          </div>
          <label class="toggle" aria-label="${escapeHtml(label)}">
            <input id="${id}" data-path="${escapeHtml(path)}" type="checkbox" ${value ? "checked" : ""} />
            <span class="toggle-track"></span>
          </label>
        </div>
      </article>`;
  }

  if (options.choices) {
    const choices = options.choices.map((choice) =>
      `<option value="${escapeHtml(choice)}" ${String(choice) === String(value) ? "selected" : ""}>${escapeHtml(choice)}</option>`
    ).join("");
    return `
      <article class="field-card${wide}">
        <label for="${id}">${escapeHtml(label)} ${pathHint(path)}</label>
        <select id="${id}" data-path="${escapeHtml(path)}">${choices}</select>${help}
      </article>`;
  }

  const structured = typeof value === "object" && value !== null;
  const multiline = options.multiline || structured;
  if (multiline) {
    const display = structured ? JSON.stringify(value, null, 2) : value;
    return `
      <article class="field-card${wide}">
        <label for="${id}">${escapeHtml(label)} ${pathHint(path)}</label>
        <textarea id="${id}" data-path="${escapeHtml(path)}" data-structured="${structured}">${escapeHtml(display)}</textarea>${help}
      </article>`;
  }

  const secret = isSecret(path);
  const configured = state.configuredSecrets.has(path);
  const inputType = secret ? "password" : (typeof value === "number" ? "number" : "text");
  const step = inputType === "number" ? (options.step || "any") : "";
  const placeholder = secret
    ? (configured ? "Configured — enter a new value to replace" : "Not configured")
    : (options.placeholder || "");
  return `
    <article class="field-card${wide}">
      <label for="${id}">${escapeHtml(label)} ${pathHint(path)}</label>
      <input id="${id}" data-path="${escapeHtml(path)}" type="${inputType}" value="${secret ? "" : escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" ${step ? `step="${step}"` : ""} />${help}
    </article>`;
}

function attachFieldListeners(root = document) {
  root.querySelectorAll("[data-path]").forEach((control) => {
    control.addEventListener("change", () => {
      let value;
      if (control.type === "checkbox") {
        value = control.checked;
      } else if (control.dataset.structured === "true") {
        try {
          value = JSON.parse(control.value);
          control.setCustomValidity("");
        } catch {
          control.setCustomValidity("Enter valid JSON");
          control.reportValidity();
          return;
        }
      } else if (control.type === "number") {
        value = Number(control.value);
      } else {
        value = control.value;
      }
      if (isSecret(control.dataset.path) && !value) return;
      updateValue(control.dataset.path, value);
    });
  });
}

function renderOverview() {
  const selected = state.config.selected_module || {};
  $("#providerSummary").innerHTML = SUMMARY_GROUPS.map((group) => `
    <article class="metric-card">
      <span>${group}</span>
      <strong title="${escapeHtml(selected[group] || "Not selected")}">${escapeHtml(selected[group] || "Not selected")}</strong>
    </article>`).join("");

  const server = state.config.server || {};
  const rows = [
    ["WebSocket", server.websocket || `ws://${server.ip || "0.0.0.0"}:${server.port || 8000}/xiaozhi/v1/`],
    ["Vision", server.vision_explain || "Not configured"],
    ["HTTP bind", `${server.ip || "0.0.0.0"}:${server.http_port || 8003}`],
    ["Settings", server.settings?.allow_remote ? "Local network access" : "This machine only"],
  ];
  $("#endpointSummary").innerHTML = rows.map(([key, value]) =>
    `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`
  ).join("");

  const websocket = rows[0][1];
  const vision = rows[1][1];
  $("#sidebarWebsocket").textContent = websocket;
  $("#sidebarWebsocket").title = websocket;
  $("#sidebarVision").textContent = vision;
  $("#sidebarVision").title = vision;
  $("#sidebarLlm").textContent = selected.LLM || "Not selected";
  $("#sidebarLlm").title = selected.LLM || "Not selected";
}

function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value);
  let unit = 0;
  while (Math.abs(amount) >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit >= 3 ? 2 : 1;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

function formatCpu(value) {
  return value === null || value === undefined ? "Sampling…" : `${Number(value).toFixed(1)}%`;
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const milliseconds = Number(value);
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds >= 10000 ? 1 : 2)} s`;
}

function formatUptime(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.floor(Number(value)));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function resourceCard(label, value, detail) {
  return `
    <article class="resource-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </article>`;
}

function meter(label, value, detail, tone = "") {
  const numeric = Math.max(0, Math.min(100, Number(value) || 0));
  return `
    <article class="resource-meter ${tone}">
      <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(detail)}</strong></div>
      <div class="meter-track"><i style="width: ${numeric}%"></i></div>
    </article>`;
}

function renderResources() {
  const resources = state.resources;
  if (!resources || !resources.available) {
    $("#resourceSummary").innerHTML = [
      resourceCard("Total CPU", "—", "Server + managed children"),
      resourceCard("Total RAM", "—", "Process-tree RSS"),
      resourceCard("System memory", "—", "Host sample unavailable"),
      resourceCard("GPU memory", "—", "Platform support required"),
      resourceCard("Local models", "—", "No sample available"),
    ].join("");
    $("#resourceMeters").innerHTML = "";
    $("#resourceStatus").classList.remove("online");
    $("#resourceStatus").innerHTML = "Unavailable";
    $("#resourceNote").textContent = resources?.reason || "Waiting for the first resource sample.";
    return;
  }

  const total = resources.total || {};
  const server = resources.server || {};
  const models = resources.local_models || {};
  const system = resources.system || {};
  const gpu = resources.gpu || {};
  const gpuValue = gpu.available ? formatBytes(gpu.memory_bytes) : "Unavailable";
  const gpuDetail = gpu.available
    ? `${gpu.backend || "GPU"} · ${gpu.process_count || 0} tracked process(es)`
    : (gpu.reason || "Per-process metrics unavailable");
  const modelValue = models.active
    ? `${models.process_count || 0} active`
    : "Stopped";

  const processMemoryDetail = total.unique_memory_bytes === null || total.unique_memory_bytes === undefined
    ? `RSS · ${total.process_count || 0} process(es)`
    : `${formatBytes(total.unique_memory_bytes)} unique · ${total.process_count || 0} process(es)`;
  const systemMemoryValue = system.memory_total_bytes
    ? `${formatBytes(system.memory_used_bytes)} / ${formatBytes(system.memory_total_bytes)}`
    : "Unavailable";

  $("#resourceSummary").innerHTML = [
    resourceCard(
      "Process CPU",
      formatCpu(total.cpu_percent),
      `Server ${formatCpu(server.cpu_percent)} · models ${formatCpu(models.cpu_percent)}`,
    ),
    resourceCard(
      "Process memory",
      formatBytes(total.memory_bytes),
      processMemoryDetail,
    ),
    resourceCard("System memory", systemMemoryValue, `${formatCpu(system.memory_percent)} used · ${formatBytes(system.memory_available_bytes)} available`),
    resourceCard("GPU memory", gpuValue, gpuDetail),
    resourceCard(
      "Local models",
      modelValue,
      models.active ? `${formatBytes(models.memory_bytes)} RSS` : "Managed llama.cpp is not running",
    ),
  ].join("");
  const logicalCpus = Math.max(1, Number(resources.logical_cpu_count) || 1);
  const processCpuCapacity = Math.min(100, (Number(total.cpu_percent) || 0) / logicalCpus);
  const processMemoryShare = system.memory_total_bytes
    ? (Number(total.memory_bytes) / Number(system.memory_total_bytes)) * 100
    : 0;
  $("#resourceMeters").innerHTML = [
    meter("System CPU", system.cpu_percent, formatCpu(system.cpu_percent)),
    meter("Process CPU capacity", processCpuCapacity, `${formatCpu(total.cpu_percent)} across ${logicalCpus} logical cores`),
    meter("System memory pressure", system.memory_percent, `${formatCpu(system.memory_percent)} used`, Number(system.memory_percent) >= 85 ? "warning" : ""),
    meter("Process share of RAM", processMemoryShare, `${processMemoryShare.toFixed(1)}% of physical memory`),
  ].join("");
  $("#resourceStatus").classList.add("online");
  $("#resourceStatus").innerHTML = "<i></i>Live";
  $("#resourceNote").textContent = (
    `Server uptime ${formatUptime(resources.server_uptime_seconds)} · system uptime ${formatUptime(system.uptime_seconds)}. `
    + "RSS includes shared pages; unique memory excludes pages shared with other processes."
  );
  renderSidebarLive();
}

function markDelta(marks, start, end) {
  const startValue = marks?.[start];
  const endValue = marks?.[end];
  if (startValue === undefined || endValue === undefined) return null;
  return Math.max(0, Number(endValue) - Number(startValue));
}

function turnPhase(label, value) {
  if (value === null || value === undefined) return "";
  return `<span><small>${escapeHtml(label)}</small>${escapeHtml(formatDuration(value))}</span>`;
}

function renderDiagnostics() {
  const runtime = state.resources?.runtime || {};
  const summary = runtime.summary || {};
  const turns = runtime.turns || [];
  const events = runtime.device_events || [];
  const connections = runtime.connections || {};

  $("#diagnosticSummary").innerHTML = [
    resourceCard("Connected robots", String(connections.active_count || 0), connections.active_count ? "WebSocket online" : "Waiting for a device"),
    resourceCard("Recent success", summary.sample_size ? `${summary.completed || 0} / ${summary.sample_size}` : "—", `${summary.attention || 0} need attention`),
    resourceCard("Median turn", formatDuration(summary.median_total_ms), "Last 20 completed records"),
    resourceCard("P95 turn", formatDuration(summary.p95_total_ms), "Slow-tail latency"),
  ].join("");

  const status = $("#diagnosticStatus");
  status.classList.toggle("online", turns.length > 0 && !summary.attention);
  status.classList.toggle("attention", Boolean(summary.attention));
  status.innerHTML = turns.length
    ? `<i></i>${summary.attention ? `${summary.attention} need attention` : "Healthy"}`
    : "<i></i>No turns";

  $("#deviceEvents").innerHTML = events.slice(0, 3).map((event) => {
    const observedAt = event.observed_at
      ? new Date(event.observed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : "—";
    const reason = event.reset_reason
      ? `Firmware reported ${event.reset_reason}${event.reset_reason_source ? ` via ${event.reset_reason_source}` : ""}.`
      : "Firmware did not report a reset reason.";
    const evidence = event.detection === "recent_disconnect"
      ? `OTA bootstrap arrived ${formatDuration(event.disconnect_before_bootstrap_ms)} after the previous WebSocket disconnected.`
      : "OTA bootstrap arrived while the previous WebSocket was still active.";
    const activeTurn = event.active_turn
      ? ` It happened during an active ${event.active_turn.source || "unknown"} turn.`
      : "";
    const vision = event.recent_vision;
    const visionDetail = vision
      ? ` Vision ${vision.request_id} was ${vision.outcome} in ${formatDuration(vision.duration_ms)}, returned ${formatBytes(vision.response_bytes)}, then bootstrap arrived ${formatDuration(vision.before_bootstrap_ms)} later.`
      : "";
    return `
      <article class="device-event warning">
        <span>!</span>
        <div>
          <header><strong>Possible device restart</strong><time>${escapeHtml(observedAt)}</time></header>
          <p>${escapeHtml(evidence + " " + reason + activeTurn + visionDetail)}</p>
        </div>
      </article>`;
  }).join("");

  if (!turns.length) {
    $("#turnFeed").innerHTML = `
      <article class="empty-state">
        <span>◌</span>
        <div><strong>No completed turns yet</strong><p>Connect the robot and finish a voice or text turn. Nothing is written to disk.</p></div>
      </article>`;
    return;
  }

  $("#turnFeed").innerHTML = turns.slice(0, 12).map((turn) => {
    const marks = turn.marks_ms || {};
    const tools = turn.tools || [];
    const outcome = turn.outcome || "unknown";
    const completedAt = turn.completed_at ? new Date(turn.completed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
    const llmFirst = markDelta(marks, "llm_request", "llm_first_response");
    const resumedLlmFirst = markDelta(marks, "resumed_llm_request", "resumed_llm_first_response");
    const asr = markDelta(marks, "asr_start", "asr_done");
    const audible = markDelta(marks, "speech_end", "first_audio_sent");
    const toolDuration = Math.max(0, ...tools.map((tool) => Number(tool.duration_ms) || 0));
    const toolMarkup = tools.length
      ? `<div class="turn-tools">${tools.map((tool) => `<span class="${escapeHtml(tool.outcome || "unknown")}">${escapeHtml(tool.name || "tool")}<small>${escapeHtml(formatDuration(tool.duration_ms))}</small></span>`).join("")}</div>`
      : "";
    return `
      <article class="turn-card ${escapeHtml(outcome)}">
        <div class="turn-rail"><i></i></div>
        <div class="turn-body">
          <header>
            <div><span class="turn-outcome">${escapeHtml(outcome.replaceAll("_", " "))}</span><time>${escapeHtml(completedAt)}</time></div>
            <strong>${escapeHtml(formatDuration(turn.total_ms))}</strong>
          </header>
          <p class="turn-input">${escapeHtml(turn.input || `${turn.source || "Unknown"} turn`)}</p>
          <div class="turn-observability">
            <div class="turn-phases">
              ${turnPhase("ASR", asr)}
              ${turnPhase("LLM first", llmFirst)}
              ${turnPhase(tools.length > 1 ? "Slowest tool" : "Tool", tools.length ? toolDuration : null)}
              ${turnPhase("Resume", resumedLlmFirst)}
              ${turnPhase("To audio", audible)}
            </div>
            ${toolMarkup}
          </div>
        </div>
      </article>`;
  }).join("");
}

function renderSidebarLive() {
  const resources = state.resources;
  const connections = resources?.runtime?.connections || {};
  const connected = Number(connections.active_count) > 0;
  const device = connections.items?.[0];
  const firmware = resources?.runtime?.devices?.find((item) => item.device_id === device?.device_id);
  $("#sidebarDevice").textContent = connected ? `${connections.active_count} connected` : "Offline";
  $("#sidebarDevice").title = device?.device_id || "No active device";
  $("#sidebarFirmware").textContent = firmware?.firmware_version || "Unknown";
  $("#sidebarFirmware").title = firmware
    ? `${firmware.device_model || "Unknown device model"}${firmware.last_reset_reason ? ` · last reset: ${firmware.last_reset_reason}` : ""}`
    : "Device model unavailable";
  $("#sidebarProcess").textContent = resources?.available
    ? `${formatCpu(resources.total?.cpu_percent)} · ${formatBytes(resources.total?.memory_bytes)}`
    : "Unavailable";
  $("#sidebarLiveDot").classList.toggle("online", connected);
}

function renderProviders() {
  const selected = state.config.selected_module || {};
  $("#providerSelectors").innerHTML = PROVIDER_GROUPS.map((group) => {
    const providers = Object.keys(state.config[group] || {});
    const options = providers.map((provider) =>
      `<option value="${escapeHtml(provider)}" ${provider === selected[group] ? "selected" : ""}>${escapeHtml(provider)}</option>`
    ).join("");
    return `<article class="provider-card"><label for="select-${group}">${group}</label><select id="select-${group}" data-provider-group="${group}">${options}</select></article>`;
  }).join("");

  $("#providerSelectors").querySelectorAll("[data-provider-group]").forEach((select) => {
    select.addEventListener("change", () => {
      updateValue(`selected_module.${select.dataset.providerGroup}`, select.value);
      renderProviders();
    });
  });

  $("#providerEditors").innerHTML = PROVIDER_GROUPS.map((group, index) => {
    const provider = selected[group];
    const config = getPath(state.config, `${group}.${provider}`, {});
    const fields = Object.keys(config).map((key) =>
      field(`${group}.${provider}.${key}`, labelFor(key))
    ).join("");
    return `
      <details class="provider-editor panel" ${index === 1 ? "open" : ""}>
        <summary><div><strong>${group} · ${escapeHtml(provider || "Not selected")}</strong><small>${Object.keys(config).length} configuration values</small></div><span class="badge">Active</span></summary>
        <div class="provider-fields">${fields || '<p class="field-help">No configurable values.</p>'}</div>
      </details>`;
  }).join("");
  attachFieldListeners($("#providerEditors"));
}

function renderAssistant() {
  $("#assistantFields").innerHTML = [
    field("prompt", "System prompt", { multiline: true, wide: true, help: "Defines personality, response style, and tool-language behavior." }),
    field("wakeup_greeting", "Wake-up greeting", { help: "Short acknowledgement sent after wake-word detection." }),
    field("exit_farewell", "Exit farewell"),
    field("system_error_response", "Error response", { multiline: true }),
    field("tool_error_response", "Tool error response", { multiline: true }),
    field("tool_timeout_response", "Tool timeout response", { multiline: true }),
    field("prompt_template", "Prompt template path"),
    field("exit_commands", "Exit commands", { help: "JSON list matched before intent processing." }),
    field("end_prompt", "Conversation ending", { help: "JSON object controlling idle conversation closure." }),
    field("wakeup_words", "Wake words", { help: "JSON list used to identify activation phrases." }),
    field("enable_greeting", "Enable greeting", { type: "boolean" }),
    field("enable_direct_answer_tool", "Enable direct-answer tool", { type: "boolean" }),
    field("enable_wakeup_words_response_cache", "Cache wake response", { type: "boolean" }),
    field("enable_stop_tts_notify", "End-of-speech notification", { type: "boolean" }),
  ].join("");
  attachFieldListeners($("#assistantFields"));
}

function renderRuntime() {
  $("#runtimeFields").innerHTML = [
    field("server.ip", "Listen address"),
    field("server.port", "WebSocket port"),
    field("server.http_port", "HTTP port"),
    field("server.websocket", "Advertised WebSocket URL"),
    field("server.vision_explain", "Vision endpoint"),
    field("server.timezone_offset", "OTA timezone offset"),
    field("server.settings.enabled", "Enable settings UI", { type: "boolean" }),
    field("server.settings.allow_remote", "Allow settings over LAN", { type: "boolean", help: "Disabled by default. Enable only on a trusted network." }),
    field("log.log_level", "Log level", { choices: ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"] }),
    field("close_connection_no_voice_time", "Idle disconnect (seconds)"),
    field("tts_timeout", "TTS timeout (seconds)"),
    field("tool_call_timeout", "Tool timeout (seconds)"),
    field("asr_min_audio_ms", "Minimum ASR audio (ms)"),
    field("asr_audio_queue_max_frames", "ASR queue limit (frames)"),
    field("tts_audio_send_delay", "Audio packet delay (ms)"),
    field("stop_tts_notify_voice", "End-of-speech sound path"),
    field("delete_audio", "Delete generated audio", { type: "boolean" }),
    field("enable_websocket_ping", "WebSocket ping", { type: "boolean" }),
    field("enable_turn_metrics", "Turn metrics", { type: "boolean" }),
    field("dump_full_llm_request", "Dump full LLM requests", { type: "boolean", help: "May write private conversation and tool data to disk." }),
    field("llm_request_dump_file", "LLM request dump path"),
  ].join("");
  attachFieldListeners($("#runtimeFields"));
}

function renderIntegrations() {
  $("#integrationFields").innerHTML = [
    field("mcp_endpoint", "External MCP endpoint", { placeholder: "ws://host:port/mcp/?token=…" }),
    field("device_mcp_tool_cache", "Device MCP tool cache", { wide: true, help: "Used only by the managed local llama.cpp provider." }),
    field("context_providers", "Context providers", { wide: true, help: "JSON list of optional HTTP context sources. Configured authorization values remain masked." }),
    field("voiceprint", "Voiceprint", { wide: true, help: "Leave the URL empty to keep voiceprint recognition disabled." }),
    field("plugins", "Server plugins", { wide: true, help: "JSON configuration for optional server-side tools." }),
  ].join("");
  attachFieldListeners($("#integrationFields"));
}

function renderAdvanced() {
  $("#advancedFields").innerHTML = [
    field("server.auth", "Device authentication", { wide: true }),
    field("server.mqtt_gateway", "MQTT gateway"),
    field("server.mqtt_signature_key", "MQTT signing key"),
    field("server.udp_gateway", "UDP gateway"),
    field("log.log_format", "Console log format", { multiline: true, wide: true }),
    field("log.log_format_file", "File log format", { multiline: true, wide: true }),
    field("log.log_dir", "Log directory"),
    field("log.log_file", "Log filename"),
    field("log.data_dir", "Runtime data directory"),
    field("xiaozhi", "Protocol hello", { wide: true }),
    field("module_test", "Benchmark defaults", { wide: true }),
  ].join("");
  attachFieldListeners($("#advancedFields"));
}

function renderAll() {
  renderOverview();
  renderProviders();
  renderAssistant();
  renderRuntime();
  renderIntegrations();
  renderAdvanced();
  renderDiagnostics();
  renderSidebarLive();
  $("#configPath").textContent = state.configPath || "data/.config.yaml";
  $("#restartPanel").classList.toggle("hidden", !state.restartRequired);
  updateDirtyState();
}

async function loadResources() {
  try {
    const response = await fetch("/api/settings/status", { cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    state.resources = await response.json();
  } catch (error) {
    state.resources = { available: false, reason: error.message };
  }
  renderResources();
  renderDiagnostics();
  renderSidebarLive();
  const delay = document.hidden ? 10000 : 2000;
  window.setTimeout(loadResources, delay);
}

function updateDirtyState() {
  const dirty = Object.keys(state.patch).length > 0;
  $("#saveButton").disabled = !dirty;
  $("#discardButton").disabled = !dirty;
  $("#saveState").textContent = dirty ? "Unsaved changes" : "No unsaved changes";
}

function toast(message, error = false) {
  const element = document.createElement("div");
  element.className = `toast${error ? " error" : ""}`;
  element.textContent = message;
  $("#toastRegion").append(element);
  setTimeout(() => element.remove(), 4200);
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    state.config = payload.config;
    state.original = clone(payload.config);
    state.patch = {};
    state.configuredSecrets = new Set(payload.configured_secrets || []);
    state.configPath = payload.config_path;
    state.restartRequired = Boolean(payload.restart_required);
    $("#apiStatus").textContent = "Ready";
    renderAll();
  } catch (error) {
    $("#apiStatus").textContent = "Unavailable";
    toast(`Could not load settings: ${error.message}`, true);
  }
}

async function saveSettings() {
  $("#saveButton").disabled = true;
  $("#saveState").textContent = "Saving…";
  try {
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: state.patch }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Save failed");
    state.config = payload.config;
    state.original = clone(payload.config);
    state.patch = {};
    state.configuredSecrets = new Set(payload.configured_secrets || []);
    state.restartRequired = Boolean(payload.restart_required);
    renderAll();
    toast("Configuration saved. Restart to apply it.");
  } catch (error) {
    updateDirtyState();
    toast(error.message, true);
  }
}

function discardChanges() {
  state.config = clone(state.original);
  state.patch = {};
  renderAll();
  toast("Unsaved changes discarded.");
}

async function restartServer() {
  if (Object.keys(state.patch).length > 0) {
    toast("Save or discard changes before restarting.", true);
    return;
  }

  $("#restartOverlay").classList.remove("hidden");
  try {
    const response = await fetch("/api/settings/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) throw new Error(await response.text());
  } catch (error) {
    $("#restartOverlay").classList.add("hidden");
    toast(`Restart failed: ${error.message}`, true);
    return;
  }

  const started = Date.now();
  const poll = async () => {
    try {
      const response = await fetch("/api/settings", { cache: "no-store" });
      if (response.ok && Date.now() - started > 1200) {
        window.location.reload();
        return;
      }
    } catch {}
    setTimeout(poll, 700);
  };
  setTimeout(poll, 900);
}

function trackNavigation() {
  const links = [...document.querySelectorAll(".navigation a")];
  const sections = links.map((link) => document.querySelector(link.getAttribute("href")));
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    links.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${visible.target.id}`));
  }, { rootMargin: "-25% 0px -65%", threshold: [0, 0.2, 0.6] });
  sections.forEach((section) => observer.observe(section));
}

$("#saveButton").addEventListener("click", saveSettings);
$("#discardButton").addEventListener("click", discardChanges);
$("#restartButton").addEventListener("click", restartServer);
$("#restartNowButton").addEventListener("click", restartServer);
trackNavigation();
loadSettings();
loadResources();
