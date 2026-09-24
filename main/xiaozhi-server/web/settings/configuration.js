import {
  $,
  escapeHtml,
  getPath,
  labelFor,
  setPath,
  state,
} from "./shared.js";
import { renderOverview } from "./resources.js";

const PROVIDER_GROUPS = ["VAD", "ASR", "LLM", "VLLM", "TTS", "Memory", "Intent"];
const SECRET_NAMES = new Set([
  "access_key", "access_key_secret", "access_token", "api_key", "auth_key",
  "authorization", "client_secret", "mqtt_signature_key", "password",
  "mcp_endpoint", "personal_access_token", "private_key", "secret", "secret_key",
  "token",
]);

let dirtyStateHandler = () => {};

export function initializeConfiguration(onDirtyStateChange) {
  dirtyStateHandler = onDirtyStateChange;
}

function updateValue(path, value) {
  setPath(state.config, path, value);
  setPath(state.patch, path, value);
  dirtyStateHandler();
  renderOverview();
}

function isSecret(path) {
  const key = path.split(".").at(-1).toLowerCase();
  return SECRET_NAMES.has(key) || key.endsWith("_token") || key.endsWith("_secret");
}

function pathHint(path) {
  return `<small>${escapeHtml(path)}</small>`;
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

function settingsGroup(title, description, fields) {
  return `
    <section class="settings-group panel">
      <header>
        <div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p></div>
        <span>${fields.length}</span>
      </header>
      <div class="form-grid">${fields.join("")}</div>
    </section>`;
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
      state.activeProviderGroup = select.dataset.providerGroup;
      updateValue(`selected_module.${select.dataset.providerGroup}`, select.value);
      renderProviders();
    });
  });

  if (!PROVIDER_GROUPS.includes(state.activeProviderGroup)) {
    state.activeProviderGroup = "LLM";
  }
  $("#providerTabs").innerHTML = PROVIDER_GROUPS.map((group) => `
    <button type="button" role="tab" data-provider-tab="${group}" aria-selected="${group === state.activeProviderGroup}">
      <span>${group}</span><small>${escapeHtml(selected[group] || "Not selected")}</small>
    </button>`).join("");
  $("#providerTabs").querySelectorAll("[data-provider-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeProviderGroup = button.dataset.providerTab;
      renderProviders();
    });
  });

  const group = state.activeProviderGroup;
  const provider = selected[group];
  const config = getPath(state.config, `${group}.${provider}`, {});
  const fields = Object.keys(config).map((key) =>
    field(`${group}.${provider}.${key}`, labelFor(key))
  ).join("");
  $("#providerEditors").innerHTML = `
    <article class="provider-workspace panel">
      <header>
        <div><p class="eyebrow">${escapeHtml(group)} provider</p><h3>${escapeHtml(provider || "Not selected")}</h3></div>
        <span class="badge">${Object.keys(config).length} values</span>
      </header>
      <div class="provider-fields">${fields || '<p class="field-help">No configurable values.</p>'}</div>
    </article>`;
  attachFieldListeners($("#providerEditors"));
}

function renderAssistant() {
  $("#assistantFields").innerHTML = [
    settingsGroup("Personality", "The prompt and template that shape every response.", [
      field("prompt", "System prompt", { multiline: true, wide: true, help: "Defines personality, response style, and tool-language behavior." }),
      field("prompt_template", "Prompt template path"),
    ]),
    settingsGroup("Spoken responses", "Short phrases used around wake, exit, and recoverable errors.", [
      field("wakeup_greeting", "Wake-up greeting", { help: "Short acknowledgement sent after wake-word detection." }),
      field("exit_farewell", "Exit farewell"),
      field("system_error_response", "Error response", { multiline: true }),
      field("tool_error_response", "Tool error response", { multiline: true }),
      field("tool_timeout_response", "Tool timeout response", { multiline: true }),
    ]),
    settingsGroup("Conversation control", "Wake and exit matching for the turn-based interaction model.", [
      field("exit_commands", "Exit commands", { help: "JSON list matched before intent processing." }),
      field("end_prompt", "Conversation ending", { help: "JSON object controlling idle conversation closure." }),
      field("wakeup_words", "Wake words", { help: "JSON list used to identify activation phrases." }),
    ]),
    settingsGroup("Behavior switches", "Optional behavior that can be enabled independently.", [
      field("enable_greeting", "Enable greeting", { type: "boolean" }),
      field("enable_direct_answer_tool", "Enable direct-answer tool", { type: "boolean" }),
      field("enable_wakeup_words_response_cache", "Cache wake response", { type: "boolean" }),
      field("enable_stop_tts_notify", "End-of-speech notification", { type: "boolean" }),
    ]),
  ].join("");
  attachFieldListeners($("#assistantFields"));
}

function renderRuntime() {
  $("#runtimeFields").innerHTML = [
    settingsGroup("Network", "Listeners, advertised endpoints, and local settings access.", [
      field("server.ip", "Listen address"),
      field("server.port", "WebSocket port"),
      field("server.http_port", "HTTP port"),
      field("server.websocket", "Advertised WebSocket URL"),
      field("server.vision_explain", "Vision endpoint"),
      field("server.timezone_offset", "OTA timezone offset"),
      field("server.settings.enabled", "Enable settings UI", { type: "boolean" }),
      field("server.settings.allow_remote", "Allow settings over LAN", { type: "boolean", help: "Disabled by default. Enable only on a trusted network." }),
      field("enable_websocket_ping", "WebSocket ping", { type: "boolean" }),
    ]),
    settingsGroup("Turn limits", "Timeouts and queue boundaries for deterministic turn cleanup.", [
      field("close_connection_no_voice_time", "Idle disconnect (seconds)"),
      field("tts_timeout", "TTS timeout (seconds)"),
      field("tool_call_timeout", "Tool timeout (seconds)"),
      field("asr_min_audio_ms", "Minimum ASR audio (ms)"),
      field("asr_audio_queue_max_frames", "ASR queue limit (frames)"),
    ]),
    settingsGroup("Audio delivery", "TTS packet pacing and generated audio retention.", [
      field("tts_audio_send_delay", "Audio packet delay (ms)"),
      field("stop_tts_notify_voice", "End-of-speech sound path"),
      field("delete_audio", "Delete generated audio", { type: "boolean" }),
    ]),
    settingsGroup("Diagnostics", "Logging and optional request inspection.", [
      field("log.log_level", "Log level", { choices: ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"] }),
      field("enable_turn_metrics", "Turn metrics", { type: "boolean" }),
      field("dump_full_llm_request", "Dump full LLM requests", { type: "boolean", help: "May write private conversation and tool data to disk." }),
      field("llm_request_dump_file", "LLM request dump path"),
    ]),
    settingsGroup("Diagnostic highlights", "Thresholds for WebUI warnings only; provider timeouts are unchanged.", [
      field("server.settings.diagnostics.thresholds_ms.llm_first", "LLM first response (ms)"),
      field("server.settings.diagnostics.thresholds_ms.resumed_llm_first", "Resumed LLM response (ms)"),
      field("server.settings.diagnostics.thresholds_ms.tool", "Tool duration (ms)"),
      field("server.settings.diagnostics.thresholds_ms.tts_first", "TTS first audio (ms)"),
      field("server.settings.diagnostics.thresholds_ms.first_audio", "Speech end to audio (ms)"),
      field("server.settings.diagnostics.thresholds_ms.total", "Total turn duration (ms)"),
    ]),
  ].join("");
  attachFieldListeners($("#runtimeFields"));
}

function renderIntegrations() {
  $("#integrationFields").innerHTML = [
    settingsGroup("MCP", "Firmware cache and optional external MCP connectivity.", [
      field("mcp_endpoint", "External MCP endpoint", { placeholder: "ws://host:port/mcp/?token=…" }),
      field("device_mcp_tool_cache", "Device MCP tool cache", { wide: true, help: "Used only by the managed local llama.cpp provider." }),
    ]),
    settingsGroup("Server context", "Optional data sources available to the assistant.", [
      field("context_providers", "Context providers", { wide: true, help: "JSON list of optional HTTP context sources. Configured authorization values remain masked." }),
      field("plugins", "Server plugins", { wide: true, help: "JSON configuration for optional server-side tools." }),
    ]),
    settingsGroup("Recognition", "Optional speaker recognition configuration.", [
      field("voiceprint", "Voiceprint", { wide: true, help: "Leave the URL empty to keep voiceprint recognition disabled." }),
    ]),
  ].join("");
  attachFieldListeners($("#integrationFields"));
}

function renderAdvanced() {
  $("#advancedFields").innerHTML = [
    settingsGroup("Authentication & gateways", "Low-level device access and external transports.", [
      field("server.auth", "Device authentication", { wide: true }),
      field("server.mqtt_gateway", "MQTT gateway"),
      field("server.mqtt_signature_key", "MQTT signing key"),
      field("server.udp_gateway", "UDP gateway"),
    ]),
    settingsGroup("Logging", "Console, file, and runtime data locations.", [
      field("log.log_format", "Console log format", { multiline: true, wide: true }),
      field("log.log_format_file", "File log format", { multiline: true, wide: true }),
      field("log.log_dir", "Log directory"),
      field("log.log_file", "Log filename"),
      field("log.data_dir", "Runtime data directory"),
    ]),
    settingsGroup("Protocol & benchmarks", "Raw hello payload and performance tester defaults.", [
      field("xiaozhi", "Protocol hello", { wide: true }),
      field("module_test", "Benchmark defaults", { wide: true }),
    ]),
  ].join("");
  attachFieldListeners($("#advancedFields"));
}

export function renderConfiguration() {
  renderProviders();
  renderAssistant();
  renderRuntime();
  renderIntegrations();
  renderAdvanced();
}
