import { $, escapeHtml, labelFor, state } from "./shared.js";

let timer = null;
let controller = null;
let generation = 0;
let active = false;
let nodes = [];

function render() {
  const selected = $("#clusterLogNode").value;
  const visible = nodes.filter((node) => selected === "all" || node.node_id === selected);
  $("#clusterVoiceSummary").innerHTML = visible.map((node) => {
    const counts = node.sessions || {};
    const phases = ["asr", "llm", "tts"].filter((phase) => counts[phase])
      .map((phase) => `${phase.toUpperCase()} ${counts[phase]}`).join(" · ");
    const restarting = counts.asr_restarting ? `ASR reconnecting ${counts.asr_restarting}` : "";
    const summary = node.state !== "ready" ? "Unavailable; state unconfirmed"
      : [phases, restarting].filter(Boolean).join(" · ") || "No active voice turn";
    return `<div><strong>${escapeHtml(node.node_id)}</strong><span>${escapeHtml(summary)}</span></div>`;
  }).join("");
  const events = visible.flatMap((node) => (node.events || []).map((entry) => ({ ...entry, node: node.node_id })))
    .sort((a, b) => a.at.localeCompare(b.at) || a.node.localeCompare(b.node) || a.seq - b.seq);
  const viewport = $("#clusterLogViewport");
  const following = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 50;
  $("#clusterLogFeed").innerHTML = events.map((entry) => {
    const fields = [entry.session_id.slice(0, 8), `turn ${entry.generation ?? "—"}`];
    if (entry.worker_id) fields.push(`worker ${entry.worker_id}`);
    if (entry.elapsed_ms !== undefined) fields.push(`${entry.elapsed_ms} ms`);
    if (entry.frames !== undefined) fields.push(`${entry.frames} frames`);
    if (entry.code) fields.push(entry.code);
    const level = entry.event === "turn_failed" ? "error" : "info";
    return `<div class="log-line cluster-log-line level-${level}"><time>${escapeHtml(new Date(entry.at).toLocaleTimeString())}</time><span class="log-source">${escapeHtml(entry.node)}</span><span>${escapeHtml(labelFor(entry.event))}</span><code>${escapeHtml(fields.join(" · "))}</code></div>`;
  }).join("") || '<div class="log-empty">No recent voice events for this selection.</div>';
  $("#clusterLogCount").textContent = `${events.length} events shown`;
  if (following) viewport.scrollTop = viewport.scrollHeight;
}

export function initializeClusterLogs() {
  $("#clusterLogNode").addEventListener("change", render);
  $("#clusterLogFollow").addEventListener("change", () => setClusterLogsActive(active));
}

export function setClusterLogsActive(value) {
  active = value;
  generation += 1;
  const current = generation;
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  controller?.abort();
  controller = null;
  if (!active || !state.controlPlane) return;
  if ($("#clusterLogFollow").value === "no") {
    $("#clusterLogStatus").textContent = "Paused";
    return;
  }
  const poll = async () => {
    const request = new AbortController();
    controller = request;
    const timeout = window.setTimeout(() => request.abort(), 4000);
    try {
      const response = await fetch("/api/cluster/diagnostics", { cache: "no-store", signal: request.signal });
      if (!response.ok) throw new Error();
      const payload = await response.json();
      if (payload.protocol !== "xiaozhi-voice-diagnostics-v1" || !Array.isArray(payload.nodes)) throw new Error();
      if (current !== generation) return;
      nodes = payload.nodes;
      const select = $("#clusterLogNode");
      const selected = select.value;
      select.innerHTML = '<option value="all">All nodes</option>' + nodes.map((node) =>
        `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.node_id)}</option>`).join("");
      select.value = nodes.some((node) => node.node_id === selected) ? selected : "all";
      render();
      const ready = nodes.filter((node) => node.state === "ready").length;
      $("#clusterLogStatus").textContent = `${ready}/${nodes.length} nodes · live${payload.scope === "local_only" ? " · local only" : ""}`;
    } catch {
      if (current === generation) {
        nodes = [];
        render();
        $("#clusterLogStatus").textContent = "Unavailable; retrying";
      }
    } finally {
      window.clearTimeout(timeout);
      if (controller === request) controller = null;
      if (current === generation && !document.hidden) timer = window.setTimeout(poll, 2000);
    }
  };
  poll();
}
