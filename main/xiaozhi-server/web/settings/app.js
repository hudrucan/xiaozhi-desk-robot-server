import {
  initializeConfiguration,
  renderConfiguration,
} from "./configuration.js?v=35";
import { renderDiagnostics } from "./diagnostics.js";
import { initializeMemory, loadMemory, renderMemory, setMemoryActive } from "./memory.js";
import {
  initializeLogs,
  renderLogs,
  setLogsActive,
} from "./logs.js";
import { initializePushTts } from "./push_tts.js";
import { renderSoundbank, soundbankAuthoringBusy } from "./soundbank.js?v=42";
import {
  renderOverview,
  renderResources,
  renderSidebarLive,
} from "./resources.js";
import { $, clone, labelFor, state, toast, escapeHtml } from "./shared.js";
import { initializeSecrets } from "./secrets.js";
import { renderCluster, setClusterActive } from "./cluster.js?v=48";
import { initializeClusterLogs, setClusterLogsActive } from "./cluster_logs.js?v=47";
import { initializeRuntimeApply, setRuntimeApplyEnabled } from "./runtime_apply.js?v=48";

const PAGE_IDS = [
  "overview",
  "diagnostics",
  "logs",
  "providers",
  "assistant",
  "soundbank",
  "memory",
  "runtime",
  "integrations",
  "advanced",
  "source",
  "cluster",
];
const STATUS_SCOPES = { overview: "overview", diagnostics: "diagnostics" };
const RUNTIME_PAGES = ["diagnostics", "soundbank"];

function applyControlPlaneMode() {
  document.querySelectorAll("[data-control-plane]").forEach((element) => {
    element.classList.toggle("hidden", !state.controlPlane);
  });
  document.querySelectorAll("[data-runtime-logs]").forEach((element) => {
    element.classList.toggle("hidden", state.controlPlane);
  });
  if (!state.controlPlane) return;
  document.querySelector('.navigation a[href="#memory"]')?.classList.toggle("hidden",
    !state.memoryCapabilities);
  $("#memoryScopeField").classList.remove("hidden");
  $("#memory .eyebrow").textContent = "Shared durable context";
  $("#memory .section-copy").textContent = "Edit a robot's shared Cloud Memory from any node. Changes sync to all nodes and affect the next recall without restarting.";
  RUNTIME_PAGES.forEach((page) => {
    document.querySelector(`.navigation a[href="#${page}"]`)?.classList.add("hidden");
  });
  document.querySelector(".sidebar-live").classList.add("hidden");
  document.querySelector(".resource-panel").classList.add("hidden");
  $("#endpointSummary").closest("article").classList.add("hidden");
  $("#providerSummary").classList.add("hidden");
  document.querySelector(".topbar h1").textContent = "Control-plane settings";
  document.querySelector(".brand small").textContent = "Cluster control plane";
  document.querySelector("#overview .badge").textContent = "Control plane";
  $("#restartPanel p").textContent = "Desired configuration differs from the last recorded conversation startup. Cluster rolling restart is not implemented.";
  ["restartButton", "restartNowButton"].forEach((id) => {
    $(`#${id}`).disabled = true;
    $(`#${id}`).textContent = "Rolling restart unavailable";
  });
  $("#sourceProvider").disabled = true;
  $("#switchSourceButton").textContent = "Source switching unavailable";
  $("#switchSourceButton").closest("article").querySelector(".section-copy").textContent =
    "This process uses the node's provisioned Cloud bootstrap. Source switching is unavailable in standalone mode.";
}

function renderAll() {
  renderOverview();
  renderSource();
  renderConfiguration();
  if (state.controlPlane) { renderCluster(); renderMemory(); }
  else {
    renderSoundbank();
    renderMemory();
    renderDiagnostics();
    renderLogs();
    renderSidebarLive();
  }
  $("#configPath").textContent = state.configurationSource.config_provider === "google_drive"
    ? `Desired: ${state.configPath} · Active/cache: local disk`
    : `Desired + active: ${state.configPath || "data/.config.yaml"}`;
  $("#restartPanel").classList.toggle("hidden", !state.restartRequired);
  updateDirtyState();
}

function renderSource() {
  const source = state.configurationSource;
  const rows = [
    ["Current provider", source.config_provider === "google_drive" ? "Google Drive" : "Local"],
    ["Node ID", source.node_id],
    ...(source.config_provider === "google_drive" ? [
      ["Config schema", source.schema_version],
      ["Settings scope", source.settings_scope === "cluster" ? "Shared cluster" : "Legacy node"],
      ["Shared cluster migration", source.cluster_migration_required ? "Explicit migration/reprovision required" : "Not required"],
      ["Desired environment", source.environment],
      ["Desired role", source.role],
      ["Active environment", source.active_environment],
      ["Active role", source.active_role],
      ["Runtime revision", source.runtime_revision],
      ["Runtime source", source.runtime_source],
    ] : []),
    ["Desired revision", source.desired_revision],
    ["Active revision", source.active_revision],
    ["Sync state", source.sync_state],
    ["Sync status", source.sync_status],
    ["Conflict", source.conflict ? "Reload required" : "None"],
    ["Last sync", source.last_sync],
    ["Last error", source.last_error],
    ["Desired source", source.source],
    ["Active source", source.active_source],
    ["Desired cache", source.cache_path],
    ["Pending provider", source.pending_provider],
  ];
  $("#sourceSummary").innerHTML = rows.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "—")}</dd></div>`).join("");
  $("#sourceSemantics").textContent = source.config_provider === "google_drive"
    ? (source.settings_scope === "cluster"
      ? "Save updates shared cluster desired configuration. Explicit node exceptions keep highest precedence. Plaintext secret changes are rejected because secrets remain node-local. Local disk retains desired cache and active LKG; restart applies the desired revision to this node."
      : "Legacy Drive source: Save retains its node/legacy scope until explicit migration to shared cluster configuration. Local disk holds desired cache and active LKG; restart applies it to this node.")
    : "Local configuration is the source of truth for desired and active configuration. Save preserves atomic local writes; runtime changes use the existing restart flow.";
  $("#sourceProvider").value = source.pending_provider || source.config_provider || "local";
  if (state.controlPlane) {
    $("#sourceSemantics").textContent =
      "Save manages shared desired configuration; node exceptions retain precedence and legacy sources keep their scope until explicit migration. Apply runtime updates installed voice workers and cores when enabled. It pauses conversations while models restart; model/dependency changes still require deployment.";
  }
  $("#switchSourceButton").disabled = state.controlPlane || Boolean(source.pending_provider);
  $("#syncSourceButton").disabled = Boolean(source.pending_provider);
}

async function syncSource() {
  if (soundbankAuthoringBusy() || state.soundbankSaving) {
    toast("Wait for soundbank authoring to finish before syncing.", true);
    return;
  }
  if (Object.keys(state.patch).length > 0 && !window.confirm(
    "Sync will discard your unsaved edits and load the latest desired configuration. Continue?"
  )) return;
  $("#syncSourceButton").disabled = true;
  try {
    const response = await fetch("/api/settings/sync", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json();
    if (payload.configuration_source) state.configurationSource = payload.configuration_source;
    if (!response.ok) throw new Error(payload.error || "Sync failed");
    if (!await loadSettings()) throw new Error("Sync succeeded, but settings reload failed. Reload before saving.");
    state.soundbankRetiredDrafts.clear();
    toast(state.controlPlane ? "Desired configuration synced; recorded active state is unchanged." : "Desired configuration synced. Active configuration changes after restart.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    renderSource();
  }
}

async function switchSource() {
  if (Object.keys(state.patch).length || soundbankAuthoringBusy() || state.soundbankSaving) {
    toast("Save or discard edits and finish soundbank authoring before switching sources.", true);
    return;
  }
  $("#switchSourceButton").disabled = true;
  try {
    const response = await fetch("/api/settings/source", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config_provider: $("#sourceProvider").value }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Source switch failed");
    state.configurationSource = payload.configuration_source;
    state.restartRequired = true;
    renderAll();
    toast("Source selected. Restart to apply it; configuration has not been copied between sources.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    renderSource();
  }
}

function activeStatusScope() {
  if (state.controlPlane) return null;
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
  if (!state.controlPlane) renderSidebarLive();
  if (activeStatusScope() && !document.hidden) loadStatus(generation);
}

function updateDirtyState() {
  const dirty = Object.keys(state.patch).length > 0;
  $("#saveButton").disabled = !dirty || Boolean(state.configurationSource.pending_provider);
  $("#discardButton").disabled = !dirty;
  $("#saveState").textContent = dirty ? "Unsaved changes" : "No unsaved changes";
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    state.memoryCapabilities = Boolean(payload.control_plane?.capabilities?.memory);
    applyControlPlaneMode();
    state.secretCapabilities = Boolean(payload.control_plane?.capabilities?.secret_provisioning);
    setRuntimeApplyEnabled(payload.control_plane?.capabilities?.runtime_apply);
    state.config = payload.config;
    state.original = clone(payload.config);
    state.patch = {};
    state.configuredSecrets = new Set(payload.configured_secrets || []);
    state.configPath = payload.config_path;
    state.configurationSource = payload.configuration_source || {};
    state.baseRevision = state.configurationSource.desired_revision;
    state.startupSoundbankDirectory = payload.soundbank_runtime_directory
      || payload.config?.static_soundbank?.directory
      || "data/soundbank";
    state.startupSoundbankAudio = payload.soundbank_runtime_audio || {
      codec: "opus",
      sample_rate: Number(payload.config?.xiaozhi?.audio_params?.sample_rate || 0),
      channels: 1,
      frame_duration_ms: 60,
    };
    state.restartRequired = Boolean(payload.restart_required);
    $("#apiStatus").textContent = "Ready";
    renderAll();
    return true;
  } catch (error) {
    $("#apiStatus").textContent = "Unavailable";
    toast(`Could not load settings: ${error.message}`, true);
    return false;
  }
}

async function saveSettings() {
  if (state.secretProvisioning) return;
  if (soundbankAuthoringBusy() || state.soundbankSaving) {
    toast("Wait for soundbank generation or cleanup to finish before saving.", true);
    return;
  }
  state.soundbankSaving = true;
  if (!state.controlPlane) renderSoundbank();
  $("#saveButton").disabled = true;
  $("#saveState").textContent = "Saving…";
  try {
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: state.patch,
        base_revision: state.baseRevision,
        ...(state.controlPlane ? {} : {
          soundbank_draft_id: state.soundbankDraftId,
          soundbank_retired_drafts: [...state.soundbankRetiredDrafts],
        }),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (payload.configuration_source) {
        state.configurationSource = payload.configuration_source;
        renderSource();
      }
      if (response.status === 409) {
        throw new Error("Configuration conflict. Your edits are retained. Sync to reload the latest desired configuration, then re-enter your changes.");
      }
      throw new Error(payload.error || "Save failed");
    }
    state.memoryCapabilities = Boolean(payload.control_plane?.capabilities?.memory);
    applyControlPlaneMode();
    state.secretCapabilities = Boolean(payload.control_plane?.capabilities?.secret_provisioning);
    setRuntimeApplyEnabled(payload.control_plane?.capabilities?.runtime_apply);
    state.config = payload.config;
    state.configPath = payload.config_path;
    state.configurationSource = payload.configuration_source || {};
    state.baseRevision = state.configurationSource.desired_revision;
    state.original = clone(payload.config);
    state.patch = {};
    state.soundbankRetiredDrafts.clear();
    state.configuredSecrets = new Set(payload.configured_secrets || []);
    state.restartRequired = Boolean(payload.restart_required);
    renderAll();
    toast(state.controlPlane ? "Desired configuration saved. Check Cluster for Soundbank audio sync on every node; runtime apply is separate." : state.restartRequired
      ? "Configuration saved. Restart to apply it."
      : "Configuration saved and applied.");
    const cleanup = payload.soundbank_cleanup;
    if (cleanup?.deleted) toast(`Cleaned ${cleanup.deleted} retired soundbank files.`);
    if (cleanup?.pending) toast(`${cleanup.pending} retired soundbank files kept while referenced; cleanup resumes after restart.`);
    if (cleanup?.errors?.length) toast(`Configuration saved; soundbank cleanup deferred: ${cleanup.errors.join("; ")}`, true);
  } catch (error) {
    updateDirtyState();
    toast(error.message, true);
  } finally {
    state.soundbankSaving = false;
    if (!state.controlPlane) renderSoundbank();
  }
}

function discardChanges() {
  if (soundbankAuthoringBusy() || state.soundbankSaving) {
    toast("Wait for soundbank generation or cleanup to finish before discarding edits.", true);
    return;
  }
  state.config = clone(state.original);
  state.patch = {};
  state.soundbankRetiredDrafts.clear();
  renderAll();
  toast("Unsaved changes discarded.");
}

async function restartServer() {
  if (state.controlPlane) {
    toast("Cluster rolling restart is not implemented.", true);
    return;
  }
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
  return PAGE_IDS.includes(page) ? page : (state.controlPlane ? "cluster" : "overview");
}

function setActivePage(page, options = {}) {
  let nextPage = PAGE_IDS.includes(page) ? page : "overview";
  if (state.controlPlane && RUNTIME_PAGES.includes(nextPage)) nextPage = "cluster";
  if (!state.controlPlane && nextPage === "cluster") nextPage = "overview";
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
  setClusterActive(state.controlPlane && nextPage === "cluster" && !document.hidden);
  setClusterLogsActive(state.controlPlane && nextPage === "logs" && !document.hidden);
  setMemoryActive(nextPage === "memory" && !document.hidden);
  if (nextPage === "memory") loadMemory();
  if (!state.controlPlane) {
    setLogsActive(nextPage === "logs" && !document.hidden);
  }
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
    if (document.hidden) {
      stopStatusPolling();
      setClusterActive(false);
      setClusterLogsActive(false);
      setMemoryActive(false);
      if (!state.controlPlane) setLogsActive(false);
    } else {
      restartStatusPolling();
      setClusterActive(state.controlPlane && state.activePage === "cluster");
      setClusterLogsActive(state.controlPlane && state.activePage === "logs");
      setMemoryActive(state.activePage === "memory");
      if (!state.controlPlane) setLogsActive(state.activePage === "logs");
    }
  });
  setActivePage(pageFromHash(), { scroll: false });
}

$("#saveButton").addEventListener("click", saveSettings);
$("#discardButton").addEventListener("click", discardChanges);
$("#restartButton").addEventListener("click", restartServer);
$("#restartNowButton").addEventListener("click", restartServer);
$("#syncSourceButton").addEventListener("click", syncSource);
$("#switchSourceButton").addEventListener("click", switchSource);
initializeConfiguration(updateDirtyState);
initializeSecrets(loadSettings);
initializeRuntimeApply();
applyControlPlaneMode();
if (state.controlPlane) initializeClusterLogs();
initializeMemory();
if (!state.controlPlane) {
  initializeLogs();
  initializePushTts();
}
initializeNavigation();
loadSettings();
