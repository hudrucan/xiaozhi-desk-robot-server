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
    ["Voice runtime", cluster.capabilities?.runtime_apply ? "See the worker/core revisions above; use Apply runtime for saved voice settings" : "Separate deployment required"],
    ["Sync state", config.sync_state],
    ["Cloud sync", config.sync_status],
    ["NATS", cluster.nats?.state],
    ["Reconciliation", cluster.reconciliation?.state],
    ["Last successful reconciliation", cluster.reconciliation?.last_success_at],
    ["Last reconciliation error", cluster.reconciliation?.last_error_at],
    ["Configured VIP", cluster.ingress?.configured_vip],
    ["Local Soundbank cache", cluster.soundbank?.state],
    ["Local Soundbank revision", cluster.soundbank?.synced_revision],
  ];
  const soundbank = state.clusterSoundbank;
  if (soundbank && soundbank.desired_revision === config.desired_revision) {
    rows.push(["Soundbank audio sync", `${soundbank.ready_nodes}/${soundbank.expected_nodes} nodes ready for revision ${soundbank.desired_revision ?? "—"}`]);
    if (soundbank.scope === "local_only") rows.push(["Soundbank membership", "Only this node is configured; cluster-wide sync is unconfirmed"]);
    for (const node of soundbank.nodes || []) {
      rows.push([`Soundbank · ${node.node_id}`, node.state === "unavailable" ? "Unavailable; sync unconfirmed"
        : `${node.state} · revision ${node.synced_revision ?? "—"} · ${node.verified_assets}/${node.expected_assets} assets`]);
    }
  }
  $("#clusterSummary").innerHTML = rows.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "—")}</dd></div>`).join("");
  $("#clusterStatusNote").textContent = "Cloud is authoritative; NATS only hints at changes. A saved configuration does not mean every node has its audio yet. Soundbank caches sync in the background. Apply runtime verifies the installed voice bundles separately from cache readiness.";
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
      try {
        const assets = await fetch("/api/cluster/soundbank", { cache: "no-store", signal: request.signal });
        if (!assets.ok) throw new Error("Soundbank status unavailable");
        const status = await assets.json();
        if (current !== generation) return;
        state.clusterSoundbank = status;
        renderCluster();
      } catch (error) {
        if (error.name !== "AbortError" && current === generation) {
          state.clusterSoundbank = null;
          renderCluster();
          $("#clusterStatusNote").textContent = "Soundbank cluster sync is unconfirmed; retrying.";
        }
      }
    } catch (error) {
      if (error.name !== "AbortError" && current === generation) {
        state.clusterSoundbank = null;
        renderCluster();
        $("#clusterStatusNote").textContent = "Cluster status unavailable; retrying.";
      }
    } finally {
      if (controller === request) controller = null;
      if (current === generation && !document.hidden) timer = window.setTimeout(poll, 5000);
    }
  };
  poll();
}
