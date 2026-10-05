import { $, escapeHtml, state } from "./shared.js";

let timer = null;
let controller = null;
let generation = 0;

export function renderCluster() {
  const cluster = state.cluster;
  if (!cluster) return;
  const config = cluster.configuration || {};
  const rows = [
    ["Settings scope", config.settings_scope === "cluster" ? "Shared cluster" : "Legacy node/source"],
    ["Current node", cluster.node_id],
    ["Desired revision", config.desired_revision],
    ["Last recorded active revision", config.active_revision],
    ["Runtime in this process", "Not started"],
    ["Restart required", config.restart_required ? "Yes; rolling restart unavailable" : "No desired/active difference"],
    ["Sync state", config.sync_state],
    ["Cloud sync", config.sync_status],
    ["NATS", cluster.nats?.state],
    ["Reconciliation", cluster.reconciliation?.state],
    ["Last successful reconciliation", cluster.reconciliation?.last_success_at],
    ["Last reconciliation error", cluster.reconciliation?.last_error_at],
    ["Configured VIP", cluster.ingress?.configured_vip],
  ];
  $("#clusterSummary").innerHTML = rows.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "—")}</dd></div>`).join("");
  $("#clusterStatusNote").textContent = "Cloud is authoritative; NATS only hints at changes. Sync loads the latest desired configuration for editing. VIP assignment/failover and cluster rolling restart are not implemented.";
}

export function setClusterActive(active) {
  generation += 1;
  const current = generation;
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  controller?.abort();
  controller = null;
  if (!active || !state.controlPlane) return;
  const poll = async () => {
    const request = new AbortController();
    controller = request;
    try {
      const response = await fetch("/api/cluster", { cache: "no-store", signal: request.signal });
      if (!response.ok) throw new Error("Cluster status unavailable");
      const payload = await response.json();
      if (current !== generation) return;
      state.cluster = payload;
      renderCluster();
    } catch (error) {
      if (error.name !== "AbortError" && current === generation) {
        $("#clusterStatusNote").textContent = "Cluster status unavailable; retrying.";
      }
    } finally {
      if (controller === request) controller = null;
      if (current === generation && !document.hidden) timer = window.setTimeout(poll, 5000);
    }
  };
  poll();
}
