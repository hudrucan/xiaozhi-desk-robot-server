export const state = {
  config: {},
  original: {},
  patch: {},
  configuredSecrets: new Set(),
  restartRequired: false,
  resources: null,
  activePage: "overview",
  activeProviderGroup: "LLM",
  statusTimer: null,
  statusGeneration: 0,
  statusController: null,
  memory: null,
  memoryLoading: false,
  memorySearch: "",
};

export const $ = (selector) => document.querySelector(selector);

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function getPath(object, path, fallback = undefined) {
  const value = path.split(".").reduce((current, key) => current?.[key], object);
  return value === undefined ? fallback : value;
}

export function setPath(object, path, value) {
  const parts = path.split(".");
  let target = object;
  parts.slice(0, -1).forEach((part) => {
    if (!target[part] || typeof target[part] !== "object" || Array.isArray(target[part])) {
      target[part] = {};
    }
    target = target[part];
  });
  target[parts.at(-1)] = value;
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function labelFor(key) {
  return key
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value);
  let unit = 0;
  while (Math.abs(amount) >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  const digits = unit >= 3 ? 2 : 1;
  return `${amount.toFixed(digits)} ${units[unit]}`;
}

export function formatCpu(value) {
  return value === null || value === undefined ? "Sampling…" : `${Number(value).toFixed(1)}%`;
}

export function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const milliseconds = Number(value);
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds >= 10000 ? 1 : 2)} s`;
}

export function formatUptime(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.floor(Number(value)));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function resourceCard(label, value, detail) {
  return `
    <article class="resource-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </article>`;
}

export function toast(message, error = false) {
  const element = document.createElement("div");
  element.className = `toast${error ? " error" : ""}`;
  element.textContent = message;
  $("#toastRegion").append(element);
  setTimeout(() => element.remove(), 4200);
}
