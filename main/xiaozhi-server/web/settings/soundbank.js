import { updateValue } from "./configuration.js?v=34";
import { $, escapeHtml, getPath, state, toast } from "./shared.js";

const NEW_ENTRY_KEY = "__soundbank_new_entry__";
const activeGenerations = new Map();
const generationErrors = new Map();

let newPhraseDraft = "";
let previewAsset = "";

function entries() {
  const value = getPath(state.config, "static_soundbank.entries", {});
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function entryFile(entry) {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object" && !Array.isArray(entry)) {
    return typeof entry.file === "string" ? entry.file : "";
  }
  return "";
}

function entryProvenance(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const provenance = entry.generated_by;
  return provenance && typeof provenance === "object" && !Array.isArray(provenance)
    ? provenance
    : null;
}

function assetError(asset) {
  if (!asset) return "Audio file is required.";
  if (/^(?:[a-zA-Z]:[\\/]|[\\/])/.test(asset) || /^[a-zA-Z][a-zA-Z\d+.-]*:/.test(asset)) {
    return "Use a relative audio path.";
  }
  if (asset.split(/[\\/]+/).includes("..")) {
    return "Audio path cannot contain '..'.";
  }
  if (!/\.(?:p3|wav|mp3)$/i.test(asset)) {
    return "Use a .p3, .wav, or .mp3 audio file.";
  }
  return "";
}

function phraseError(phrase, originalKey = "") {
  if (!phrase) return "Phrase is required.";
  const duplicate = Object.keys(entries()).some(
    (candidate) => candidate !== originalKey && candidate === phrase,
  );
  return duplicate ? "Phrase must be unique." : "";
}

function generationStatus() {
  const soundbankDirty = Object.prototype.hasOwnProperty.call(
    state.patch,
    "static_soundbank",
  );
  if (soundbankDirty) {
    return { label: "Save, then restart", className: "attention" };
  }
  if (state.restartRequired) {
    return { label: "Restart required", className: "attention" };
  }
  return { label: "Restart after config changes", className: "" };
}

function refreshApplyStatus() {
  const element = $("#soundbankApplyStatus");
  if (!element) return;
  const restart = generationStatus();
  element.className = `badge ${restart.className}`;
  element.innerHTML = `<i></i>${escapeHtml(restart.label)}`;
}

function displayGeneratedAt(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function provenanceMarkup(entry) {
  const provenance = entryProvenance(entry);
  if (!provenance) {
    return `
      <div class="soundbank-provenance manual">
        <strong>Manual</strong>
        <span>Provenance unavailable</span>
      </div>`;
  }

  const settings = provenance.settings && typeof provenance.settings === "object"
    ? provenance.settings
    : {};
  const model = provenance.model ?? settings.model ?? settings.model_name;
  const voice = provenance.voice ?? settings.voice ?? settings.private_voice;
  return `
    <div class="soundbank-provenance">
      <strong>Generated</strong>
      <span>Provider <code>${escapeHtml(provenance.provider || "Unknown")}</code></span>
      ${model !== undefined ? `<span>Model <code>${escapeHtml(model)}</code></span>` : ""}
      ${voice !== undefined ? `<span>Voice <code>${escapeHtml(voice)}</code></span>` : ""}
      <span>${escapeHtml(displayGeneratedAt(provenance.generated_at))}</span>
    </div>`;
}

function generationButton(label, action, key, mode, disabled = false) {
  const busyMode = activeGenerations.get(key);
  const loading = busyMode === mode;
  return `
    <button class="button secondary${loading ? " loading" : ""}" data-soundbank-action="${action}" type="button" ${disabled || busyMode ? "disabled" : ""}>
      <span class="soundbank-button-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(loading ? "Generating…" : label)}</span>
    </button>`;
}

function entryCard(phrase, entry, index) {
  const file = entryFile(entry);
  const provenance = entryProvenance(entry);
  const busy = activeGenerations.has(phrase);
  const error = generationErrors.get(phrase) || "";
  const invalid = Boolean(phraseError(phrase, phrase) || assetError(file));
  const rowId = `soundbank-entry-${index}`;
  return `
    <article class="soundbank-entry-card${busy ? " generating" : ""}" data-soundbank-entry data-original-key="${escapeHtml(phrase)}">
      <div class="soundbank-entry-fields">
        <label class="soundbank-entry-phrase" for="${rowId}-phrase">
          <span>Phrase</span>
          <input id="${rowId}-phrase" data-soundbank-phrase value="${escapeHtml(phrase)}" ${busy ? "disabled" : ""} />
        </label>
        <label for="${rowId}-file">
          <span>Asset filename</span>
          <input id="${rowId}-file" data-soundbank-file value="${escapeHtml(file)}" spellcheck="false" ${busy ? "disabled" : ""} />
        </label>
      </div>
      <footer class="soundbank-entry-footer">
        ${provenanceMarkup(entry)}
        <div class="soundbank-entry-actions">
          <button class="button secondary" data-soundbank-action="preview" type="button" ${busy || invalid ? "disabled" : ""}>Preview</button>
          ${generationButton("Regenerate same", "regenerate-same", phrase, "same", invalid || !provenance)}
          ${generationButton("Generate with current TTS", "generate-current", phrase, "current", invalid)}
          <button class="button secondary soundbank-remove" data-soundbank-action="remove" type="button" ${busy ? "disabled" : ""}>Remove</button>
        </div>
      </footer>
      <p class="soundbank-row-error${error ? " visible" : ""}" data-soundbank-error aria-live="polite">${escapeHtml(error)}</p>
    </article>`;
}

function previewUrl(filename) {
  const encoded = filename
    .split(/[\\/]+/)
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `/api/settings/soundbank/audio/${encoded}`;
}

function renderWorkspace() {
  const configEntries = entries();
  const entryList = Object.entries(configEntries);
  const enabled = Boolean(getPath(state.config, "static_soundbank.enabled", false));
  const directory = getPath(state.config, "static_soundbank.directory", "data/soundbank");
  const restart = generationStatus();
  const newBusy = activeGenerations.has(NEW_ENTRY_KEY);
  const newError = generationErrors.get(NEW_ENTRY_KEY) || "";
  const newPhraseValidation = newPhraseDraft.trim()
    ? phraseError(newPhraseDraft.trim())
    : "";
  const newDisabled = !newPhraseDraft.trim() || Boolean(newPhraseValidation) || newBusy;

  return `
    <div class="soundbank-layout">
      <section class="settings-group panel soundbank-settings-panel">
        <header>
          <div>
            <h3>Soundbank settings</h3>
            <p>Exact-match responses served from local audio assets.</p>
          </div>
          <span>${entryList.length}</span>
        </header>
        <div class="form-grid">
          <article class="field-card toggle-card">
            <div class="toggle-row">
              <div class="toggle-copy">
                <div class="field-label"><span>Enabled</span><small>static_soundbank.enabled</small></div>
                <p class="field-help">Use matching local audio before the configured TTS provider.</p>
              </div>
              <label class="toggle" aria-label="Enable static soundbank">
                <input id="soundbankEnabled" type="checkbox" ${enabled ? "checked" : ""} />
                <span class="toggle-track"></span>
              </label>
            </div>
          </article>
          <article class="field-card">
            <label for="soundbankDirectory">Directory <small>static_soundbank.directory</small></label>
            <input id="soundbankDirectory" value="${escapeHtml(directory)}" placeholder="data/soundbank" spellcheck="false" />
            <p class="field-help">Entry paths are relative to this directory. Generated filenames are assigned by the server.</p>
          </article>
        </div>
        <footer class="soundbank-settings-footer">
          <span class="badge ${restart.className}" id="soundbankApplyStatus"><i></i>${escapeHtml(restart.label)}</span>
          <p>Generation uses the TTS configuration loaded at server startup.</p>
        </footer>
      </section>

      <section class="panel soundbank-compose-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">New entry</p>
            <h3>Generate a local response</h3>
            <p class="field-help">Save and restart provider changes before generating with the newly configured TTS.</p>
          </div>
        </div>
        <form id="soundbankNewForm" class="soundbank-new-form">
          <label for="soundbankNewPhrase">
            <span>Phrase</span>
            <input id="soundbankNewPhrase" value="${escapeHtml(newPhraseDraft)}" placeholder="What should trigger this audio?" maxlength="512" ${newBusy ? "disabled" : ""} />
          </label>
          ${generationButton("Generate with current TTS", "generate-new", NEW_ENTRY_KEY, "current", newDisabled)}
          <p class="soundbank-row-error${newError || newPhraseValidation ? " visible" : ""}" id="soundbankNewError" aria-live="polite">${escapeHtml(newError || newPhraseValidation)}</p>
        </form>
        <div class="soundbank-preview">
          <div>
            <span class="soundbank-label">Shared preview</span>
            <strong>${previewAsset ? escapeHtml(previewAsset) : "Select an entry to preview"}</strong>
          </div>
          <audio id="soundbankAudio" controls preload="none" ${previewAsset ? `src="${escapeHtml(previewUrl(previewAsset))}"` : ""}></audio>
          <p id="soundbankPreviewStatus" class="field-help" aria-live="polite">${previewAsset ? "Ready to play." : "Preview uses the saved/generated file in the running server's soundbank directory."}</p>
        </div>
      </section>

      <section class="panel soundbank-list-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">Authoring</p>
            <h3>Entries</h3>
            <p class="field-help">Phrase and asset edits update local Settings state. Paths must be relative, exclude <code>..</code>, and use <code>.p3</code>, <code>.wav</code>, or <code>.mp3</code>.</p>
          </div>
          <span class="badge">${entryList.length} ${entryList.length === 1 ? "entry" : "entries"}</span>
        </div>
        <div class="soundbank-entry-list" id="soundbankEntries">
          ${entryList.length
            ? entryList.map(([phrase, entry], index) => entryCard(phrase, entry, index)).join("")
            : '<p class="soundbank-empty">No static soundbank entries configured.</p>'}
        </div>
      </section>
    </div>`;
}

function validateRow(row) {
  const originalKey = row.dataset.originalKey;
  const phraseInput = row.querySelector("[data-soundbank-phrase]");
  const fileInput = row.querySelector("[data-soundbank-file]");
  const errorElement = row.querySelector("[data-soundbank-error]");
  const phrase = phraseInput.value.trim();
  const file = fileInput.value.trim();
  const phraseMessage = phraseError(phrase, originalKey);
  const fileMessage = phraseMessage ? "" : assetError(file);
  const message = phraseMessage || fileMessage;

  phraseInput.setCustomValidity(phraseMessage);
  fileInput.setCustomValidity(fileMessage);
  errorElement.textContent = message;
  errorElement.classList.toggle("visible", Boolean(message));
  row.classList.toggle("invalid", Boolean(message));

  const originalEntry = entries()[originalKey];
  const manuallyChanged = phrase !== originalKey || file !== entryFile(originalEntry).trim();
  const canRegenerateSame = Boolean(entryProvenance(originalEntry)) && !manuallyChanged;
  row.querySelectorAll('[data-soundbank-action="generate-current"]').forEach((button) => {
    button.disabled = Boolean(message);
  });
  row.querySelectorAll('[data-soundbank-action="regenerate-same"]').forEach((button) => {
    button.disabled = Boolean(message) || !canRegenerateSame;
  });
  row.querySelectorAll('[data-soundbank-action="preview"]').forEach((button) => {
    button.disabled = Boolean(message);
  });
  return { valid: !message, phrase, file, originalKey, originalEntry };
}

function commitRow(row, reportInvalid = false) {
  const result = validateRow(row);
  if (!result.valid) {
    if (reportInvalid) row.querySelector(":invalid")?.reportValidity();
    return null;
  }

  const updatedEntries = {};
  let updatedEntry = result.originalEntry;
  const phraseChanged = result.phrase !== result.originalKey;
  const fileChanged = result.file !== entryFile(result.originalEntry).trim();
  if (!phraseChanged && !fileChanged) {
    return { phrase: result.phrase, entry: result.originalEntry };
  }
  if (result.originalEntry && typeof result.originalEntry === "object" && !Array.isArray(result.originalEntry)) {
    updatedEntry = { ...result.originalEntry, file: result.file };
    if (phraseChanged || fileChanged) delete updatedEntry.generated_by;
  } else {
    updatedEntry = result.file;
  }

  Object.entries(entries()).forEach(([phrase, entry]) => {
    if (phrase === result.originalKey) updatedEntries[result.phrase] = updatedEntry;
    else updatedEntries[phrase] = entry;
  });
  updateValue("static_soundbank.entries", updatedEntries);
  generationErrors.delete(result.originalKey);
  row.dataset.originalKey = result.phrase;
  row.querySelector("[data-soundbank-phrase]").value = result.phrase;
  row.querySelector("[data-soundbank-file]").value = result.file;
  refreshApplyStatus();

  if (phraseChanged || fileChanged) {
    row.querySelector(".soundbank-provenance").outerHTML = provenanceMarkup(updatedEntry);
  }
  validateRow(row);
  return { phrase: result.phrase, entry: updatedEntry };
}

function removeEntry(phrase) {
  const updatedEntries = {};
  Object.entries(entries()).forEach(([candidate, entry]) => {
    if (candidate !== phrase) updatedEntries[candidate] = entry;
  });
  if (previewAsset === entryFile(entries()[phrase])) previewAsset = "";
  generationErrors.delete(phrase);
  updateValue("static_soundbank.entries", updatedEntries);
  renderSoundbank();
}

async function generateEntry(key, phrase, mode, generatedBy = null) {
  if (activeGenerations.has(key)) return;
  activeGenerations.set(key, mode);
  generationErrors.delete(key);
  renderSoundbank();

  try {
    const response = await fetch("/api/settings/soundbank/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: phrase,
        mode,
        ...(mode === "same" ? { generated_by: generatedBy } : {}),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Soundbank generation failed");
    if (!payload.entry || typeof payload.entry !== "object" || Array.isArray(payload.entry)) {
      throw new Error("Soundbank generation returned an invalid entry");
    }

    const updatedEntries = {};
    if (key === NEW_ENTRY_KEY) {
      Object.assign(updatedEntries, entries(), { [phrase]: payload.entry });
      newPhraseDraft = "";
    } else {
      Object.entries(entries()).forEach(([candidate, entry]) => {
        updatedEntries[candidate] = candidate === key ? payload.entry : entry;
      });
    }
    updateValue("static_soundbank.entries", updatedEntries);
    toast(`Generated soundbank audio for “${phrase}”. Save changes to persist the entry.`);
  } catch (error) {
    generationErrors.set(key, error.message || "Soundbank generation failed");
  } finally {
    activeGenerations.delete(key);
    renderSoundbank();
  }
}

function attachRowListeners(row) {
  row.querySelectorAll("[data-soundbank-phrase], [data-soundbank-file]").forEach((input) => {
    input.addEventListener("input", () => {
      generationErrors.delete(row.dataset.originalKey);
      validateRow(row);
    });
    input.addEventListener("change", () => commitRow(row));
  });

  row.querySelectorAll("[data-soundbank-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.soundbankAction;
      if (action === "remove") {
        removeEntry(row.dataset.originalKey);
        return;
      }
      const committed = commitRow(row, true);
      if (!committed) return;

      if (action === "preview") {
        previewAsset = entryFile(committed.entry);
        renderSoundbank();
        const audio = $("#soundbankAudio");
        audio?.play().catch(() => {
          const status = $("#soundbankPreviewStatus");
          if (status) status.textContent = "Preview is ready; press play to start audio.";
        });
      } else if (action === "generate-current") {
        generateEntry(committed.phrase, committed.phrase, "current");
      } else if (action === "regenerate-same") {
        const provenance = entryProvenance(committed.entry);
        if (!provenance) {
          generationErrors.set(committed.phrase, "This entry has no generation provenance.");
          renderSoundbank();
          return;
        }
        generateEntry(committed.phrase, committed.phrase, "same", provenance);
      }
    });
  });
}

function attachListeners() {
  $("#soundbankEnabled")?.addEventListener("change", (event) => {
    updateValue("static_soundbank.enabled", event.currentTarget.checked);
    renderSoundbank();
  });
  $("#soundbankDirectory")?.addEventListener("change", (event) => {
    const value = event.currentTarget.value.trim() || "data/soundbank";
    updateValue("static_soundbank.directory", value);
    renderSoundbank();
  });

  document.querySelectorAll("[data-soundbank-entry]").forEach(attachRowListeners);

  const newInput = $("#soundbankNewPhrase");
  newInput?.addEventListener("input", () => {
    newPhraseDraft = newInput.value;
    generationErrors.delete(NEW_ENTRY_KEY);
    const message = phraseError(newPhraseDraft.trim());
    newInput.setCustomValidity(message);
    $("#soundbankNewError").textContent = message;
    $("#soundbankNewError").classList.toggle("visible", Boolean(message));
    const button = $('#soundbankNewForm [data-soundbank-action="generate-new"]');
    if (button) button.disabled = !newPhraseDraft.trim() || Boolean(message);
  });
  $('#soundbankNewForm [data-soundbank-action="generate-new"]')?.addEventListener("click", () => {
    $("#soundbankNewForm")?.requestSubmit();
  });
  $("#soundbankNewForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const phrase = newPhraseDraft.trim();
    const message = phraseError(phrase);
    if (message) {
      newInput.setCustomValidity(message);
      newInput.reportValidity();
      return;
    }
    generateEntry(NEW_ENTRY_KEY, phrase, "current");
  });

  const audio = $("#soundbankAudio");
  audio?.addEventListener("error", () => {
    const status = $("#soundbankPreviewStatus");
    if (status) {
      status.textContent = "Preview failed. Check that the asset exists in the configured directory.";
      status.classList.add("soundbank-error-text");
    }
  });
}

export function renderSoundbank() {
  const workspace = $("#soundbankWorkspace");
  if (!workspace) return;
  workspace.innerHTML = renderWorkspace();
  attachListeners();
}
