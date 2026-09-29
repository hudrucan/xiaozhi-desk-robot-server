import { $, formatBytes, state, toast } from "./shared.js";

const INITIAL_LINES = 300;
const BATCH_LINES = 100;
const CLIENT_MAX_LINES = 1000;
const POLL_INTERVAL_MS = 750;
const LEVEL_RANK = {
  TRACE: 0,
  DEBUG: 10,
  INFO: 20,
  SUCCESS: 25,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

let entries = [];
let cursor = 0;
let runId = null;
let timer = null;
let controller = null;
let generation = 0;
let active = false;
let paused = false;
let lastBuffer = null;
let lastError = null;

function selectedProviderIsManagedLocal(group) {
  const selected = state.config.selected_module?.[group];
  const provider = state.config[group]?.[selected];
  return Boolean(
    provider
    && provider.type === "llama_cpp"
    && provider.process?.managed !== false
  );
}

function updateSourceAvailability() {
  const source = $("#logSource");
  const localLlm = source.querySelector('option[value="local_llm"]');
  const localVllm = source.querySelector('option[value="local_vllm"]');
  localLlm.disabled = !selectedProviderIsManagedLocal("LLM");
  localVllm.disabled = !selectedProviderIsManagedLocal("VLLM");
  localLlm.textContent = localLlm.disabled ? "Local LLM · disabled" : "Local LLM";
  localVllm.textContent = localVllm.disabled ? "Local VLLM · disabled" : "Local VLLM";
  if (source.selectedOptions[0]?.disabled) source.value = "all";
}

function matchesFilters(entry) {
  const source = $("#logSource").value;
  if (source !== "all" && entry.source !== source) return false;
  if (source === "all" && entry.source === "local_llm"
      && !selectedProviderIsManagedLocal("LLM")) return false;
  if (source === "all" && entry.source === "local_vllm"
      && !selectedProviderIsManagedLocal("VLLM")) return false;

  const minimum = $("#logLevel").value;
  const entryRank = LEVEL_RANK[entry.level] ?? LEVEL_RANK.INFO;
  if (entryRank < (LEVEL_RANK[minimum] ?? LEVEL_RANK.INFO)) return false;

  const query = $("#logSearch").value.trim().toLocaleLowerCase();
  if (!query) return true;
  return `${entry.tag || ""} ${entry.message || ""}`
    .toLocaleLowerCase()
    .includes(query);
}

function formatTimestamp(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "--:--:--.---";
  return `${date.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })}.${String(date.getMilliseconds()).padStart(3, "0")}`;
}

function sourceLabel(source) {
  return {
    server: "SERVER",
    local_llm: "LLM",
    local_vllm: "VLLM",
  }[source] || String(source || "LOG").toUpperCase();
}

function buildLogLine(entry) {
  const row = document.createElement("div");
  row.className = `log-line level-${String(entry.level || "info").toLowerCase()}`;
  row.dataset.sequence = String(entry.sequence || 0);

  const time = document.createElement("time");
  time.dateTime = entry.timestamp || "";
  time.textContent = formatTimestamp(entry.timestamp);

  const source = document.createElement("span");
  source.className = `log-source source-${entry.source || "server"}`;
  source.textContent = sourceLabel(entry.source);

  const level = document.createElement("span");
  level.className = "log-level";
  level.textContent = entry.level || "INFO";

  const message = document.createElement("code");
  const tag = entry.tag ? `[${entry.tag}] ` : "";
  message.textContent = `${tag}${entry.message || ""}`;

  row.append(time, source, level, message);
  return row;
}

function scrollToLatest() {
  if ($("#logAutoscroll").checked) {
    const viewport = $("#logViewport");
    viewport.scrollTop = viewport.scrollHeight;
  }
}

function emptyMessage() {
  return entries.length
    ? "No current-run logs match these filters."
    : "Waiting for current-run logs. Previous server runs are not loaded.";
}

function renderFeed() {
  const feed = $("#logFeed");
  const visible = entries.filter(matchesFilters);
  const fragment = document.createDocumentFragment();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = emptyMessage();
    fragment.append(empty);
  } else {
    visible.forEach((entry) => fragment.append(buildLogLine(entry)));
  }
  feed.replaceChildren(fragment);
  updateMeta(visible.length);
  scrollToLatest();
}

function appendIncoming(incoming) {
  const feed = $("#logFeed");
  feed.querySelector(".log-empty")?.remove();
  const fragment = document.createDocumentFragment();
  incoming.filter(matchesFilters).forEach((entry) => {
    fragment.append(buildLogLine(entry));
  });
  feed.append(fragment);

  const earliestRetained = entries[0]?.sequence ?? cursor;
  while (
    feed.firstElementChild
    && Number(feed.firstElementChild.dataset.sequence || 0) < earliestRetained
  ) {
    feed.firstElementChild.remove();
  }
  while (feed.children.length > CLIENT_MAX_LINES) {
    feed.firstElementChild.remove();
  }
  if (!feed.children.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = emptyMessage();
    feed.append(empty);
  }
  updateMeta(feed.querySelectorAll(".log-line").length);
  scrollToLatest();
}

function updateMeta(visibleCount = entries.filter(matchesFilters).length) {
  const bufferLines = lastBuffer?.lines ?? 0;
  const bufferBytes = lastBuffer?.bytes ?? 0;
  const maximumBytes = lastBuffer?.max_bytes ?? 2 * 1024 * 1024;
  $("#logCount").textContent = `${visibleCount} shown · ${bufferLines} buffered`;
  $("#logUsage").textContent = `${formatBytes(bufferBytes)} / ${formatBytes(maximumBytes)}`;
  $("#logDropped").textContent = lastBuffer?.dropped_total
    ? `${lastBuffer.dropped_total} old line(s) evicted`
    : "No buffer eviction";

  const status = $("#logStatus");
  status.classList.toggle("online", active && !paused && !lastError);
  if (paused) status.innerHTML = "<i></i>Paused";
  else if (lastError) status.innerHTML = "<i></i>Unavailable";
  else if (active) status.innerHTML = "<i></i>Live";
  else status.innerHTML = "<i></i>Current run";
}

function stopPolling() {
  if (timer !== null) {
    window.clearTimeout(timer);
    timer = null;
  }
  if (controller) {
    controller.abort();
    controller = null;
  }
}

function schedulePoll(currentGeneration, delay = POLL_INTERVAL_MS) {
  if (!active || paused || document.hidden || currentGeneration !== generation) return;
  timer = window.setTimeout(() => loadLogs(currentGeneration), delay);
}

async function loadLogs(currentGeneration = generation) {
  if (!active || paused || document.hidden || currentGeneration !== generation) return;
  const requestController = new AbortController();
  controller = requestController;
  const firstLoad = cursor === 0;
  const params = new URLSearchParams({
    limit: String(firstLoad ? INITIAL_LINES : BATCH_LINES),
  });
  if (!firstLoad) params.set("after", String(cursor));

  try {
    const response = await fetch(`/api/settings/logs?${params}`, {
      cache: "no-store",
      signal: requestController.signal,
    });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    if (currentGeneration !== generation) return;

    const runChanged = Boolean(runId && payload.run_id !== runId);
    if (runChanged) {
      entries = [];
      cursor = 0;
    }
    runId = payload.run_id;
    lastBuffer = payload.buffer || null;
    lastError = null;

    if (runChanged) {
      renderFeed();
      schedulePoll(currentGeneration, 40);
      return;
    }
    const replaceFeed = firstLoad || payload.cursor_reset;
    if (payload.cursor_reset) entries = [];
    const incoming = Array.isArray(payload.entries) ? payload.entries : [];
    entries.push(...incoming);
    if (entries.length > CLIENT_MAX_LINES) {
      entries = entries.slice(-CLIENT_MAX_LINES);
    }
    if (incoming.length) cursor = incoming.at(-1).sequence;
    else cursor = Math.max(cursor, Number(payload.latest_sequence) || 0);
    if (replaceFeed) renderFeed();
    else if (incoming.length) appendIncoming(incoming);
    else updateMeta();
    schedulePoll(currentGeneration, payload.has_more ? 40 : POLL_INTERVAL_MS);
  } catch (error) {
    if (error.name === "AbortError") return;
    lastError = error.message;
    updateMeta();
    schedulePoll(currentGeneration, 2000);
  } finally {
    if (controller === requestController) controller = null;
  }
}

function setPaused(nextPaused) {
  paused = nextPaused;
  $("#logPauseButton").textContent = paused ? "Resume" : "Pause";
  if (paused) {
    stopPolling();
  } else if (active) {
    generation += 1;
    loadLogs(generation);
  }
  updateMeta();
}

export function setLogsActive(nextActive) {
  active = Boolean(nextActive);
  stopPolling();
  generation += 1;
  updateMeta();
  if (active && !paused && !document.hidden) loadLogs(generation);
}

export function renderLogs() {
  updateSourceAvailability();
  renderFeed();
}

export function initializeLogs() {
  $("#logSource").addEventListener("change", renderFeed);
  $("#logLevel").addEventListener("change", renderFeed);
  $("#logSearch").addEventListener("input", renderFeed);
  $("#logPauseButton").addEventListener("click", () => setPaused(!paused));
  $("#logClearButton").addEventListener("click", () => {
    entries = [];
    renderFeed();
    toast("Current log view cleared. New lines will continue to appear.");
  });
  $("#logAutoscroll").addEventListener("change", renderFeed);
  $("#logViewport").addEventListener("scroll", () => {
    const viewport = $("#logViewport");
    const awayFromBottom = viewport.scrollHeight
      - viewport.scrollTop
      - viewport.clientHeight > 80;
    if (awayFromBottom && $("#logAutoscroll").checked) {
      $("#logAutoscroll").checked = false;
    }
  });
  renderLogs();
}
