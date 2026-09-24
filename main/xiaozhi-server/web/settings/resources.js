import {
  $,
  escapeHtml,
  formatBytes,
  formatCpu,
  formatUptime,
  resourceCard,
  state,
} from "./shared.js";

const SUMMARY_GROUPS = ["VAD", "ASR", "LLM", "VLLM", "TTS"];

export function renderOverview() {
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

function meter(label, value, detail, tone = "") {
  const numeric = Math.max(0, Math.min(100, Number(value) || 0));
  return `
    <article class="resource-meter ${tone}">
      <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(detail)}</strong></div>
      <div class="meter-track"><i style="width: ${numeric}%"></i></div>
    </article>`;
}

export function renderResources() {
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
  const systemMemoryInUse = Number.isFinite(Number(system.memory_total_bytes))
    && Number.isFinite(Number(system.memory_available_bytes))
    ? Math.max(0, Number(system.memory_total_bytes) - Number(system.memory_available_bytes))
    : system.memory_used_bytes;
  const systemMemoryValue = system.memory_total_bytes
    ? `${formatBytes(systemMemoryInUse)} / ${formatBytes(system.memory_total_bytes)}`
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

export function renderSidebarLive() {
  const resources = state.resources;
  const connections = resources?.runtime?.connections || {};
  const hasRuntimeSample = Boolean(resources?.runtime?.sampled_at);
  const connected = Number(connections.active_count) > 0;
  const device = connections.items?.[0];
  const firmware = resources?.runtime?.devices?.find((item) => item.device_id === device?.device_id);
  $("#sidebarLiveLabel").textContent = state.activePage === "overview"
    ? "Live resources"
    : (state.activePage === "diagnostics" ? "Live turns" : "Last sample");
  $("#sidebarDevice").textContent = connected
    ? `${connections.active_count} connected`
    : (hasRuntimeSample ? "Offline" : "Not sampled");
  $("#sidebarDevice").title = device?.device_id || "No active device";
  $("#sidebarFirmware").textContent = firmware?.firmware_version || "Unknown";
  $("#sidebarFirmware").title = firmware
    ? `${firmware.device_model || "Unknown device model"}${firmware.last_reset_reason ? ` · last reset: ${firmware.last_reset_reason}` : ""}`
    : "Device model unavailable";
  const processSample = resources?.available
    ? `${formatCpu(resources.total?.cpu_percent)} · ${formatBytes(resources.total?.memory_bytes)}`
    : "Not sampled";
  $("#sidebarProcess").textContent = state.activePage === "diagnostics"
    ? "Overview only"
    : (state.activePage === "overview" && !resources?.available ? "Unavailable" : processSample);
  $("#sidebarLiveDot").classList.toggle("online", connected);
}
