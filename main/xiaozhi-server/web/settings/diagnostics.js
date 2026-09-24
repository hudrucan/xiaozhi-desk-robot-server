import {
  $,
  escapeHtml,
  formatBytes,
  formatDuration,
  getPath,
  resourceCard,
  state,
} from "./shared.js";

const DEFAULT_THRESHOLDS_MS = {
  llm_first: 5000,
  resumed_llm_first: 5000,
  tool: 5000,
  tts_first: 2500,
  first_audio: 8000,
  total: 30000,
};

function diagnosticThresholds() {
  const configured = getPath(
    state.config,
    "server.settings.diagnostics.thresholds_ms",
    {},
  );
  return Object.fromEntries(
    Object.entries(DEFAULT_THRESHOLDS_MS).map(([key, fallback]) => {
      const value = Number(configured?.[key]);
      return [key, Number.isFinite(value) && value > 0 ? value : fallback];
    }),
  );
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

function toolStage(tool) {
  const type = String(tool.type || "unknown").replaceAll("_", " ");
  if (tool.type !== "device_mcp") return type;
  if (tool.outcome === "timed_out" || tool.outcome === "incomplete") {
    if (tool.request_sent_ms === undefined) return "before firmware dispatch";
    if (tool.response_received_ms === undefined) return "waiting for firmware";
    return "after firmware response";
  }
  if (tool.mcp_response_outcome === "error") return "firmware error";
  return tool.response_received_ms === undefined ? "device MCP" : "firmware replied";
}

function diagnosticText(value, truncated, emptyText) {
  if (!value) return `<p class="diagnostic-empty">${escapeHtml(emptyText)}</p>`;
  return `<pre>${escapeHtml(value)}${truncated ? "\n… preview truncated" : ""}</pre>`;
}

function renderToolDetail(tool) {
  const stages = [
    turnPhase("Turn offset", tool.started_ms),
    turnPhase("FW dispatch", tool.request_sent_ms),
    turnPhase("FW response", tool.response_received_ms),
    turnPhase("Complete", tool.duration_ms),
  ].join("");
  const identifiers = [
    tool.call_id ? `LLM ${tool.call_id}` : "",
    tool.mcp_request_id !== undefined ? `MCP ${tool.mcp_request_id}` : "",
  ].filter(Boolean).join(" · ");
  const resultState = [tool.action, toolStage(tool), formatDuration(tool.duration_ms)]
    .filter(Boolean)
    .join(" · ");
  return `
    <article class="tool-detail ${escapeHtml(tool.outcome || "unknown")}">
      <header>
        <div><strong>${escapeHtml(tool.name || "tool")}</strong><span>${escapeHtml(String(tool.type || "unknown").replaceAll("_", " "))}</span></div>
        <small>${escapeHtml(resultState)}</small>
      </header>
      <div class="tool-lifecycle">${stages}</div>
      ${identifiers ? `<code>${escapeHtml(identifiers)}</code>` : ""}
      <div class="tool-payloads">
        <section><label>Arguments</label>${diagnosticText(tool.arguments, tool.arguments_truncated, "No arguments")}</section>
        <section><label>Result</label>${diagnosticText(tool.result || tool.response, tool.result_truncated || tool.response_truncated, "No result captured")}</section>
      </div>
    </article>`;
}

function turnInsights(turn) {
  const marks = turn.marks_ms || {};
  const tools = turn.tools || [];
  const thresholds = diagnosticThresholds();
  const insights = [];
  const llmFirst = markDelta(marks, "llm_request", "llm_first_response");
  const resumedLlmFirst = markDelta(marks, "resumed_llm_request", "resumed_llm_first_response");
  const audible = markDelta(marks, "speech_end", "first_audio_sent");
  const ttsFirst = markDelta(marks, "tts_infer_start", "tts_first_opus");
  const slowestTool = Math.max(0, ...tools.map((tool) => Number(tool.duration_ms) || 0));

  if (turn.outcome && turn.outcome !== "completed") {
    insights.push({ tone: "error", label: `Turn ${String(turn.outcome).replaceAll("_", " ")}` });
  }
  if (tools.some((tool) => tool.outcome && tool.outcome !== "completed")) {
    insights.push({ tone: "error", label: "Tool needs attention" });
  }
  if (llmFirst !== null && llmFirst >= thresholds.llm_first) {
    insights.push({ tone: "warning", label: `Slow LLM ${formatDuration(llmFirst)}` });
  }
  if (resumedLlmFirst !== null && resumedLlmFirst >= thresholds.resumed_llm_first) {
    insights.push({ tone: "warning", label: `Slow resume ${formatDuration(resumedLlmFirst)}` });
  }
  if (slowestTool >= thresholds.tool) {
    insights.push({ tone: "warning", label: `Slow tool ${formatDuration(slowestTool)}` });
  }
  if (ttsFirst !== null && ttsFirst >= thresholds.tts_first) {
    insights.push({ tone: "warning", label: `Slow TTS ${formatDuration(ttsFirst)}` });
  }
  if (audible !== null && audible >= thresholds.first_audio) {
    insights.push({ tone: "warning", label: `Late audio ${formatDuration(audible)}` });
  }
  if (Number(turn.total_ms) >= thresholds.total) {
    insights.push({ tone: "warning", label: `Long turn ${formatDuration(turn.total_ms)}` });
  }
  return insights;
}

function renderTurnInsights(insights) {
  if (!insights.length) return "";
  return `<div class="turn-insights">${insights.map((insight) =>
    `<span class="${escapeHtml(insight.tone)}">${escapeHtml(insight.label)}</span>`
  ).join("")}</div>`;
}

export function renderDiagnostics() {
  const expandedTurns = new Set(
    [...document.querySelectorAll(".turn-details[open][data-turn-id]")]
      .map((details) => details.dataset.turnId),
  );
  const runtime = state.resources?.runtime || {};
  const summary = runtime.summary || {};
  const turns = runtime.turns || [];
  const events = runtime.device_events || [];
  const connections = runtime.connections || {};
  const recentTurns = turns.slice(0, 20);
  const flaggedTurns = recentTurns.filter((turn) => turnInsights(turn).length > 0).length;

  $("#diagnosticSummary").innerHTML = [
    resourceCard("Connected robots", String(connections.active_count || 0), connections.active_count ? "WebSocket online" : "Waiting for a device"),
    resourceCard("Recent success", summary.sample_size ? `${summary.completed || 0} / ${summary.sample_size}` : "—", `${flaggedTurns} flagged by latency or outcome`),
    resourceCard("Median turn", formatDuration(summary.median_total_ms), "Last 20 completed records"),
    resourceCard("P95 turn", formatDuration(summary.p95_total_ms), "Slow-tail latency"),
  ].join("");

  const status = $("#diagnosticStatus");
  status.classList.toggle("online", turns.length > 0 && !flaggedTurns);
  status.classList.toggle("attention", Boolean(flaggedTurns));
  status.innerHTML = turns.length
    ? `<i></i>${flaggedTurns ? `${flaggedTurns} flagged` : "Healthy"}`
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
    const insights = turnInsights(turn);
    const toolMarkup = tools.length
      ? `<div class="turn-tools">${tools.map((tool) => `<span class="${escapeHtml(tool.outcome || "unknown")}">${escapeHtml(tool.name || "tool")}<small>${escapeHtml(toolStage(tool))} · ${escapeHtml(formatDuration(tool.duration_ms))}</small></span>`).join("")}</div>`
      : "";
    const detailLabel = [turn.input ? "input" : "", turn.output ? "output" : "", tools.length ? `${tools.length} tool${tools.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
    const turnId = String(turn.turn_id || turn.sentence_id || turn.completed_at || "");
    const details = detailLabel
      ? `<details class="turn-details" data-turn-id="${escapeHtml(turnId)}" ${expandedTurns.has(turnId) ? "open" : ""}>
          <summary><span>Inspect turn</span><small>${escapeHtml(detailLabel)}</small></summary>
          <div class="turn-transcript">
            <section><label>User input</label>${diagnosticText(turn.input, turn.input_truncated, "Input was not captured")}</section>
            <section><label>Assistant output</label>${diagnosticText(turn.output, turn.output_truncated, "Output was not captured")}</section>
          </div>
          ${tools.length ? `<div class="tool-details">${tools.map(renderToolDetail).join("")}</div>` : ""}
        </details>`
      : "";
    return `
      <article class="turn-card ${escapeHtml(outcome)}${insights.length ? " flagged" : ""}">
        <div class="turn-rail"><i></i></div>
        <div class="turn-body">
          <header>
            <div><span class="turn-outcome">${escapeHtml(outcome.replaceAll("_", " "))}</span><time>${escapeHtml(completedAt)}</time></div>
            <strong>${escapeHtml(formatDuration(turn.total_ms))}</strong>
          </header>
          <div class="turn-observability">
            <div class="turn-phases">
              ${turnPhase("ASR", asr)}
              ${turnPhase("LLM first", llmFirst)}
              ${turnPhase(tools.length > 1 ? "Slowest tool" : "Tool", tools.length ? toolDuration : null)}
              ${turnPhase("Resume", resumedLlmFirst)}
              ${turnPhase("To audio", audible)}
            </div>
            ${toolMarkup}
            ${renderTurnInsights(insights)}
          </div>
          ${details}
        </div>
      </article>`;
  }).join("");
}
