import { $, escapeHtml, state, toast } from "./shared.js";

function memoryTime(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function renderMemoryEntries() {
  const container = $("#memoryEntries");
  const memory = state.memory || {};
  const entries = Array.isArray(memory.entries) ? memory.entries : [];
  const query = state.memorySearch.trim().toLocaleLowerCase();
  const filtered = query
    ? entries.filter((entry) => String(entry.content || "").toLocaleLowerCase().includes(query))
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
    <article class="memory-entry" data-memory-entry="${escapeHtml(entry.id || "")}">
      <textarea maxlength="${Number(memory.entry_max_chars) || 300}" aria-label="Memory content">${escapeHtml(entry.content || "")}</textarea>
      <footer>
        <span>Updated ${escapeHtml(memoryTime(entry.updated_at || entry.created_at))}</span>
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
        { content: entry.querySelector("textarea").value },
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
  $("#memoryContent").disabled = !ready || state.memoryLoading;
  $("#memoryCreateButton").disabled = !ready || state.memoryLoading;
  $("#memorySearch").disabled = !ready || state.memoryLoading;
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
    });
  });
}
