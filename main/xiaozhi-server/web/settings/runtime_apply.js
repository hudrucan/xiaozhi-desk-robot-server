import { $, state, toast, escapeHtml } from "./shared.js";

let enabled = false;
let timer = null;
let controller = null;
let status = null;
let submitting = false;
const phaseLabels = {
  preparing: "Checking configuration", prepare: "Checking configuration",
  quiesce: "Pausing conversations", install: "Installing configuration",
  worker: "Starting voice workers", core: "Starting conversation cores",
  finish: "Verifying runtime", recovering: "Restoring previous runtime",
  restore: "Restoring configuration", old_worker: "Starting previous workers",
  old_core: "Starting previous cores", rolled_back: "Previous runtime restored",
  verified: "Verified",
};

function render() {
  $("#applyRuntimeButton").classList.toggle("hidden", !enabled);
  $("#runtimeApplyPanel").classList.toggle("hidden", !enabled);
  if (state.controlPlane) $("#controlPlaneBanner").classList.toggle("hidden", enabled);
  if (!enabled) return;
  const busy = submitting || status?.job?.state === "running";
  $("#applyRuntimeButton").textContent = busy ? "Applying runtime…"
    : status?.recovery_required ? "Recover runtime" : "Apply runtime";
  // A recovery/apply action stays available when meaningful. A fully applied
  // revision has a plain status instead of an inert action in the toolbar.
  const applied = status?.ready_nodes === 3 && status?.desired_revision === state.baseRevision;
  $("#applyRuntimeButton").classList.toggle("hidden", !busy && applied && !status?.recovery_required);
  $("#applyRuntimeButton").disabled = busy;
  $("#runtimeApplyHeading").textContent = applied ? `Runtime revision ${status.desired_revision} · 3/3 nodes`
    : `Saved revision ${state.baseRevision ?? "—"} · ${status?.ready_nodes ?? 0}/3 applied`;
  const job = status?.job;
  let text = "Save changes first, then Apply runtime. Conversations pause while voice workers restart. Settings and MQTT stay available.";
  if (busy) text = `${phaseLabels[job?.phase] || "Applying runtime"}${job?.node_id ? ` · ${job.node_id}` : ""}. Keep all three nodes powered on.`;
  else if (status?.recovery_required) text = "An interrupted apply needs recovery. Recover runtime restores the previous bundles before another apply.";
  else if (job?.state === "failed") text = `Apply did not complete${job.node_id ? ` on ${job.node_id}` : ""} (${phaseLabels[job.phase] || "verification"}). Previous runtime restored. Check configuration, local keys and installed models before retrying.`;
  else if (job?.state === "rolled_back") text = "Previous runtime restored. You can now apply the saved configuration.";
  else if (applied) text = "All three voice workers and conversation cores report the saved revision. New conversations use this configuration.";
  $("#runtimeApplyNote").textContent = text;
  $("#runtimeApplyNodes").innerHTML = (status?.nodes || []).map(node =>
    `<div><dt>${escapeHtml(node.node_id)}</dt><dd>Worker ${escapeHtml(node.worker_revision ?? "—")} · Core ${escapeHtml(node.core_revision ?? "—")}${node.state === "unavailable" ? " · Unavailable" : ""}</dd></div>`).join("");
}

async function poll() {
  window.clearTimeout(timer);
  if (!enabled || document.hidden || controller) return;
  const current = controller = new AbortController();
  try {
    const response = await fetch("/api/settings/runtime", { cache: "no-store", signal: current.signal });
    const payload = await response.json();
    if (!response.ok || payload.protocol !== "xiaozhi-runtime-apply-v1") throw new Error("Runtime status unavailable");
    status = payload;
    render();
  } catch (error) {
    if (error.name !== "AbortError") $("#runtimeApplyNote").textContent = "Runtime status unavailable. Keep all three nodes powered on and retry.";
  } finally {
    if (controller === current) controller = null;
    if (enabled && !document.hidden) timer = window.setTimeout(poll, status?.job?.state === "running" ? 2000 : 6000);
  }
}

export function setRuntimeApplyEnabled(value) {
  enabled = state.controlPlane && Boolean(value);
  render();
  if (enabled) poll();
  else {
    window.clearTimeout(timer);
    controller?.abort();
  }
}

async function apply() {
  if (submitting || status?.job?.state === "running") return;
  if (Object.keys(state.patch).length || state.secretProvisioning || state.soundbankSaving) {
    toast("Save or discard edits before applying runtime.", true);
    return;
  }
  const recover = Boolean(status?.recovery_required);
  // The action's maintenance effect is described beside the revision. This
  // executes only saved configuration, never unsaved browser fields.
  submitting = true;
  render();
  try {
    const response = await fetch("/api/settings/runtime", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Xiaozhi-Settings": "1" },
      body: JSON.stringify({ revision: state.baseRevision, recover }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Runtime apply unavailable");
    status = { ...status, job: payload.job };
    toast(recover ? "Runtime recovery started." : "Runtime apply started. Conversations pause until verification finishes.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    submitting = false;
    render();
    poll();
  }
}

export function initializeRuntimeApply() {
  $("#applyRuntimeButton").addEventListener("click", apply);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      window.clearTimeout(timer);
      controller?.abort();
    } else poll();
  });
}
