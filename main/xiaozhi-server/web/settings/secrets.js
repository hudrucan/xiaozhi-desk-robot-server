import { $, escapeHtml, state, toast } from "./shared.js";

let reloadSettings = async () => false;

export function initializeSecrets(reload) {
  reloadSettings = reload;
}

export function clusterSecretField(group, provider, field) {
  const enabled = Boolean(state.secretCapabilities);
  const id = `cluster-key-${group}-${provider}`;
  return `<article class="field-card secret-card" data-cluster-secret
      data-group="${escapeHtml(group)}" data-provider="${escapeHtml(provider)}" data-field="${escapeHtml(field)}">
    <label for="${escapeHtml(id)}">API key <span class="secret-scope">Shared · 3 nodes</span></label>
    <div class="secret-entry">
      <input id="${escapeHtml(id)}" type="password" data-secret-value autocomplete="new-password" spellcheck="false"
        placeholder="Enter API key" ${enabled && !state.secretProvisioning ? "" : "disabled"} />
      <button class="button primary" type="button" data-provision-secret ${enabled && !state.secretProvisioning ? "" : "disabled"}>Save to 3 nodes</button>
    </div>
    <div class="secret-nodes" data-secret-status role="status" aria-live="polite">${enabled
      ? '<span class="secret-note">Checking nodes…</span>'
      : '<span class="secret-note">Key provisioning unavailable</span>'}</div>
    <p class="field-help secret-help">Stored privately on each node. Saved keys stay hidden.</p>
  </article>`;
}

function renderNodes(card, payload) {
  if (!card.isConnected) return;
  const labels = { ready: "Saved", stored: "Saved", missing: "No key",
    not_configured: "No key", unconfirmed: "Unavailable" };
  card.querySelector("[data-secret-status]").innerHTML = (payload.nodes || []).map((node) => {
    const ready = node.state === "ready" || node.state === "stored";
    const unavailable = node.state === "unconfirmed";
    return `<span class="secret-node ${ready ? "ready" : unavailable ? "unavailable" : "missing"}">
      <span class="secret-dot" aria-hidden="true"></span><strong>${escapeHtml(node.node_id)}</strong>
      <span>${escapeHtml(labels[node.state] || "Unavailable")}</span></span>`;
  }).join("");
  if (payload.ready && !payload.consistent) {
    card.querySelector("[data-secret-status]").insertAdjacentHTML("beforeend", '<span class="secret-note">Node credentials differ</span>');
  }
}

function target(card) {
  return { group: card.dataset.group, provider: card.dataset.provider, field: card.dataset.field };
}

async function checkReadiness(card) {
  try {
    const response = await fetch(`/api/settings/secrets?${new URLSearchParams(target(card))}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error("Key status unavailable");
    renderNodes(card, payload);
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
      if (payload.nodes?.length) renderNodes(card, payload);
      else if (card.isConnected) card.querySelector("[data-secret-status]").textContent = "Save not confirmed. Sync before retrying.";
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
