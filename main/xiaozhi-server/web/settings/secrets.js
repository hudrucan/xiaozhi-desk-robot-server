import { $, escapeHtml, state, toast } from "./shared.js";

let reloadSettings = async () => false;

export function initializeSecrets(reload) {
  reloadSettings = reload;
}

export function clusterSecretField(group, provider, field, label, path) {
  const enabled = Boolean(state.secretCapabilities);
  return `<article class="field-card wide" data-cluster-secret
      data-group="${escapeHtml(group)}" data-provider="${escapeHtml(provider)}" data-field="${escapeHtml(field)}">
    <label>${escapeHtml(label)} <small>${escapeHtml(path)}</small>
      <input type="password" data-secret-value autocomplete="new-password" spellcheck="false"
        placeholder="Enter a new key; stored keys are never displayed" ${enabled && !state.secretProvisioning ? "" : "disabled"} />
    </label>
    <button type="button" data-provision-secret ${enabled && !state.secretProvisioning ? "" : "disabled"}>Save key on all 3 nodes</button>
    <p class="field-help" data-secret-status>${enabled
      ? "Checking node readiness…"
      : "Cluster key provisioning is not enabled on this deployment."}</p>
    <p class="field-help">Use this trusted LAN Settings connection. Save ordinary edits first. A key change takes effect in desired configuration only after every node confirms storage.</p>
  </article>`;
}

function nodeSummary(payload) {
  const labels = { ready: "ready", stored: "stored", missing: "missing key",
    not_configured: "not configured", unconfirmed: "not confirmed" };
  return (payload.nodes || []).map((node) => `${node.node_id}: ${labels[node.state] || "unavailable"}`).join(" · ");
}

function target(card) {
  return { group: card.dataset.group, provider: card.dataset.provider, field: card.dataset.field };
}

async function checkReadiness(card) {
  try {
    const response = await fetch(`/api/settings/secrets?${new URLSearchParams(target(card))}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error("Key status unavailable");
    if (card.isConnected) card.querySelector("[data-secret-status]").textContent = nodeSummary(payload)
      + (payload.ready && !payload.consistent ? " · Node credential exceptions differ" : "");
  } catch {
    if (card.isConnected) card.querySelector("[data-secret-status]").textContent = "Key status unavailable. Sync Settings and check node availability.";
  }
}

async function provision(card) {
  if (state.secretProvisioning) return;
  if (Object.keys(state.patch).length || state.soundbankSaving) {
    toast("Save or discard ordinary edits before saving the cluster key.", true);
    return;
  }
  const input = card.querySelector("[data-secret-value]");
  let value = input.value;
  if (!value || /\s/.test(value)) {
    toast("Enter a nonempty API key without whitespace.", true);
    return;
  }
  state.secretProvisioning = true;
  // Do not put the credential into state.config/state.patch, local storage or a URL.
  const controls = [...document.querySelectorAll("input, textarea, select, button")]
    .filter((control) => !control.disabled);
  controls.forEach((control) => { control.disabled = true; });
  card.querySelector("[data-secret-status]").textContent = "Saving privately on every node…";
  try {
    const response = await fetch("/api/settings/secrets", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Xiaozhi-Settings": "1" },
      body: JSON.stringify({ ...target(card), value, base_revision: state.baseRevision }),
    });
    value = "";
    input.value = "";
    const payload = await response.json();
    if (!response.ok) {
      if (card.isConnected) card.querySelector("[data-secret-status]").textContent = nodeSummary(payload) || "Save not confirmed; sync before retrying.";
      throw new Error(payload.error || "Key save was not confirmed");
    }
    if (!payload.committed) throw new Error("Key save was not confirmed. Sync before retrying.");
    if (!await reloadSettings()) {
      toast("Key committed on all nodes. Reload Settings before making another change.", true);
      return;
    }
    toast("Key saved on all 3 nodes. Running workers are unchanged.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    value = "";
    input.value = "";
    state.secretProvisioning = false;
    controls.forEach((control) => { if (control.isConnected) control.disabled = false; });
    document.querySelectorAll("[data-cluster-secret] input, [data-provision-secret]")
      .forEach((control) => { control.disabled = !state.secretCapabilities; });
  }
}

export function bindClusterSecretFields(root) {
  root.querySelectorAll("[data-cluster-secret]").forEach((card) => {
    card.querySelector("[data-provision-secret]").addEventListener("click", () => provision(card));
    if (state.secretCapabilities) checkReadiness(card);
  });
}
