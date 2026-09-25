import { $, escapeHtml, state, toast } from "./shared.js";

const DEFAULT_TYPES = [
  "fact",
  "preference",
  "decision",
  "project_state",
  "hardware",
  "todo",
  "session",
];

function memoryTime(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function typeOptions(types, selected) {
  return types.map((type) => `<option value="${escapeHtml(type)}"${type === selected ? " selected" : ""}>${escapeHtml(type.replaceAll("_", " "))}</option>`).join("");
}

function importanceOptions(selected) {
  return [1, 2, 3, 4, 5].map((value) => `<option value="${value}"${Number(selected) === value ? " selected" : ""}>${value}</option>`).join("");
}

function commaList(value) {
  return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function entryPayload(entry) {
  return {
    content: entry.querySelector("textarea").value,
    type: entry.querySelector("[data-memory-type]").value,
    project: entry.querySelector("[data-memory-project]").value,
    entities: commaList(entry.querySelector("[data-memory-entities]").value),
    tags: commaList(entry.querySelector("[data-memory-tags]").value),
    importance: Number(entry.querySelector("[data-memory-importance]").value),
    pinned: entry.querySelector("[data-memory-pinned]").checked,
    active: entry.querySelector("[data-memory-active]").checked,
  };
}

function renderMemoryEntries() {
  const container = $("#memoryEntries");
  const memory = state.memory || {};
  const entries = Array.isArray(memory.entries) ? memory.entries : [];
  const types = Array.isArray(memory.memory_types) ? memory.memory_types : DEFAULT_TYPES;
  const query = state.memorySearch.trim().toLocaleLowerCase();
  const filtered = query
    ? entries.filter((entry) => [
      entry.content,
      entry.type,
      entry.project,
      ...(entry.entities || []),
      ...(entry.tags || []),
    ].join(" ").toLocaleLowerCase().includes(query))
    : entries;

  $("#memoryCount").textContent = `${entries.length} memor${entries.length === 1 ? "y" : "ies"}`;
  if (!memory.available || !memory.initialized) {
    container.innerHTML = `<article class="empty-state memory-empty"><span>◌</span><div><strong>No memory scope yet</strong><p>${escapeHtml(memory.reason || "Connect the robot once, or keep one stored device scope in the local memory file.")}</p></div></article>`;
    return;
  }
  if (!filtered.length) {
    const message = entries.length ? "No memories match this filter." : "No explicit memories have been saved yet.";
    container.innerHTML = `<article class="empty-state memory-empty"><span>◌</span><div><strong>Nothing to show</strong><p>${escapeHtml(message)}</p></div></article>`;
    return;
  }

  container.innerHTML = filtered.map((entry) => `
    <article class="memory-entry${entry.active ? "" : " inactive"}" data-memory-entry="${escapeHtml(entry.id || "")}">
      <textarea maxlength="${Number(memory.entry_max_chars) || 300}" aria-label="Memory content">${escapeHtml(entry.content || "")}</textarea>
      <div class="memory-entry-fields">
        <label><span>Type</span><select data-memory-type>${typeOptions(types, entry.type || "fact")}</select></label>
        <label><span>Project</span><input data-memory-project value="${escapeHtml(entry.project || "")}" placeholder="Global when empty" /></label>
        <label><span>Entities</span><input data-memory-entities value="${escapeHtml((entry.entities || []).join(", "))}" placeholder="camera, vl53l0x" /></label>
        <label><span>Tags</span><input data-memory-tags value="${escapeHtml((entry.tags || []).join(", "))}" placeholder="sensor, distance" /></label>
        <label><span>Importance</span><select data-memory-importance>${importanceOptions(entry.importance || 3)}</select></label>
      </div>
      <div class="memory-entry-toggles">
        <label class="memory-check"><input data-memory-pinned type="checkbox"${entry.pinned ? " checked" : ""} /><span>Pinned</span></label>
        <label class="memory-check"><input data-memory-active type="checkbox"${entry.active ? " checked" : ""} /><span>Active</span></label>
      </div>
      <footer>
        <span class="memory-entry-meta">Updated ${escapeHtml(memoryTime(entry.updated_at || entry.created_at))} · ${escapeHtml(entry.id || "no id")}${entry.supersedes ? ` · supersedes ${escapeHtml(entry.supersedes)}` : ""}</span>
        <div>
          <button class="button secondary" type="button" data-memory-delete>Delete</button>
          <button class="button primary" type="button" data-memory-save>Save</button>
        </div>
      </footer>
    </article>`).join("");

  container.querySelectorAll("[data-memory-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const entry = button.closest("[data-memory-entry]");
      await mutateMemory(
        "PUT",
        `/api/settings/memory/${encodeURIComponent(entry.dataset.memoryEntry)}`,
        entryPayload(entry),
      );
    });
  });
  container.querySelectorAll("[data-memory-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      const entry = button.closest("[data-memory-entry]");
      if (!window.confirm("Delete this memory? This cannot be undone.")) return;
      await mutateMemory(
        "DELETE",
        `/api/settings/memory/${encodeURIComponent(entry.dataset.memoryEntry)}`,
      );
    });
  });
}

export function renderMemory() {
  const memory = state.memory || {};
  const status = $("#memoryStatus");
  const ready = Boolean(memory.available && memory.initialized);
  status.classList.toggle("online", ready);
  status.classList.toggle("attention", Boolean(state.memory && !ready));
  status.innerHTML = state.memoryLoading
    ? "<i></i>Loading"
    : `<i></i>${ready ? "Ready" : (state.memory ? "Unavailable" : "Not loaded")}`;

  $("#memoryContent").maxLength = Number(memory.entry_max_chars) || 300;
  ["#memoryContent", "#memoryType", "#memoryProject", "#memoryImportance", "#memoryPinned", "#memoryCreateButton", "#memorySearch"].forEach((selector) => {
    $(selector).disabled = !ready || state.memoryLoading;
  });
  $("#memoryContext").textContent = ready
    ? `${memory.scope_source === "storage" ? "Stored scope" : "Device scope"} ${memory.device_id || "active"} · recall ${memory.recall_enabled ? "on" : "off"} · ${memory.max_entries} max`
    : (memory.reason || "No unambiguous stored device scope is available.");
  $("#memorySearch").value = state.memorySearch;
  renderMemoryEntries();
}

export async function loadMemory() {
  if (state.memoryLoading) return;
  state.memoryLoading = true;
  renderMemory();
  try {
    const response = await fetch("/api/settings/memory", { cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    state.memory = await response.json();
  } catch (error) {
    state.memory = { available: false, reason: error.message, entries: [] };
  } finally {
    state.memoryLoading = false;
    renderMemory();
  }
}

async function mutateMemory(method, url, body = null) {
  try {
    const options = { method, headers: { "Content-Type": "application/json" } };
    if (body !== null) options.body = JSON.stringify(body);
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(await response.text());
    state.memory = await response.json();
    $("#memoryContent").value = "";
    renderMemory();
    toast("Memory updated.");
  } catch (error) {
    toast(error.message || "Memory update failed", true);
  }
}

export function initializeMemory() {
  $("#memoryRefreshButton").addEventListener("click", loadMemory);
  $("#memorySearch").addEventListener("input", (event) => {
    state.memorySearch = event.target.value;
    renderMemoryEntries();
  });
  $("#memoryCreateForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await mutateMemory("POST", "/api/settings/memory", {
      content: $("#memoryContent").value,
      type: $("#memoryType").value,
      project: $("#memoryProject").value,
      importance: Number($("#memoryImportance").value),
      pinned: $("#memoryPinned").checked,
    });
  });
}
