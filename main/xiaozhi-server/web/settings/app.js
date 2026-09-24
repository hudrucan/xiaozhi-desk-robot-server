import {
  initializeConfiguration,
  renderConfiguration,
} from "./configuration.js";
import { renderDiagnostics } from "./diagnostics.js";
import { initializeMemory, loadMemory, renderMemory } from "./memory.js";
import {
  renderOverview,
  renderResources,
  renderSidebarLive,
} from "./resources.js";
import { $, clone, labelFor, state, toast } from "./shared.js";

const PAGE_IDS = [
  "overview",
  "diagnostics",
  "providers",
  "assistant",
  "memory",
  "runtime",
  "integrations",
  "advanced",
];
const STATUS_SCOPES = { overview: "overview", diagnostics: "diagnostics" };

function renderAll() {
  renderOverview();
  renderConfiguration();
  renderMemory();
  renderDiagnostics();
  renderSidebarLive();
  $("#configPath").textContent = state.configPath || "data/.config.yaml";
  $("#restartPanel").classList.toggle("hidden", !state.restartRequired);
  updateDirtyState();
}

function activeStatusScope() {
  return STATUS_SCOPES[state.activePage] || null;
}

function stopStatusPolling() {
  if (state.statusTimer !== null) {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = null;
  }
  if (state.statusController) {
    state.statusController.abort();
    state.statusController = null;
  }
}

function scheduleStatusPoll(generation) {
  if (generation !== state.statusGeneration || document.hidden) return;
  const delay = state.activePage === "diagnostics" ? 2000 : 3000;
  state.statusTimer = window.setTimeout(() => loadStatus(generation), delay);
}

async function loadStatus(generation = state.statusGeneration) {
  const scope = activeStatusScope();
  if (!scope || generation !== state.statusGeneration) return;
  const controller = new AbortController();
  state.statusController = controller;
  try {
    const response = await fetch(`/api/settings/status?scope=${scope}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    if (generation !== state.statusGeneration) return;
    if (scope === "overview") {
      state.resources = payload;
      renderResources();
    } else {
      state.resources = { ...(state.resources || {}), runtime: payload.runtime };
      renderDiagnostics();
    }
    renderSidebarLive();
  } catch (error) {
    if (error.name !== "AbortError" && generation === state.statusGeneration) {
      if (scope === "overview") {
        state.resources = { available: false, reason: error.message };
        renderResources();
      }
    }
  } finally {
    if (state.statusController === controller) state.statusController = null;
    scheduleStatusPoll(generation);
  }
}

function restartStatusPolling() {
  stopStatusPolling();
  state.statusGeneration += 1;
  const generation = state.statusGeneration;
  renderSidebarLive();
  if (activeStatusScope() && !document.hidden) loadStatus(generation);
}

function updateDirtyState() {
  const dirty = Object.keys(state.patch).length > 0;
  $("#saveButton").disabled = !dirty;
  $("#discardButton").disabled = !dirty;
  $("#saveState").textContent = dirty ? "Unsaved changes" : "No unsaved changes";
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
    toast(state.restartRequired
      ? "Configuration saved. Restart to apply it."
      : "Configuration saved and applied.");
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

function pageFromHash() {
  const page = window.location.hash.slice(1);
  return PAGE_IDS.includes(page) ? page : "overview";
}

function setActivePage(page, options = {}) {
  const nextPage = PAGE_IDS.includes(page) ? page : "overview";
  state.activePage = nextPage;
  document.querySelectorAll(".page-section").forEach((section) => {
    section.classList.toggle("active", section.id === nextPage);
  });
  document.querySelectorAll(".navigation a").forEach((link) => {
    const active = link.getAttribute("href") === `#${nextPage}`;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (options.updateHash && window.location.hash !== `#${nextPage}`) {
    window.history.pushState(null, "", `#${nextPage}`);
  }
  document.title = `${labelFor(nextPage)} · Xiaozhi Server`;
  if (options.scroll !== false) window.scrollTo({ top: 0, behavior: "auto" });
  restartStatusPolling();
  if (nextPage === "memory") loadMemory();
}

function initializeNavigation() {
  document.querySelectorAll('.navigation a, .brand[href^="#"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      const page = link.getAttribute("href").slice(1);
      if (!PAGE_IDS.includes(page)) return;
      event.preventDefault();
      setActivePage(page, { updateHash: true });
    });
  });
  window.addEventListener("popstate", () => setActivePage(pageFromHash()));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopStatusPolling();
    else restartStatusPolling();
  });
  setActivePage(pageFromHash(), { scroll: false });
}

$("#saveButton").addEventListener("click", saveSettings);
$("#discardButton").addEventListener("click", discardChanges);
$("#restartButton").addEventListener("click", restartServer);
$("#restartNowButton").addEventListener("click", restartServer);
initializeConfiguration(updateDirtyState);
initializeMemory();
initializeNavigation();
loadSettings();
