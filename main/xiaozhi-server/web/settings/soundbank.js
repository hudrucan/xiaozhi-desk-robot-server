import { updateValue } from "./configuration.js?v=34";
import { $, escapeHtml, formatBytes, getPath, state, toast } from "./shared.js";

const NEW_ENTRY_KEY = "__soundbank_new_entry__";
const P3_FRAME_DURATION_MS = 60;
const EMOJI_CODE_POINT_RANGES = [
  [0x1F1E6, 0x1F1FF],
  [0x1F300, 0x1F5FF],
  [0x1F600, 0x1F64F],
  [0x1F680, 0x1F6FF],
  [0x1F900, 0x1F9FF],
  [0x1FA70, 0x1FAFF],
  [0x2600, 0x26FF],
  [0x2700, 0x27BF],
];
const EMOJI_SEQUENCE_CODE_POINTS = new Set([0x200D, 0x20E3, 0xFE0F]);
const activeGenerations = new Map();
const activeOptimizations = new Set();
const generationErrors = new Map();

let newPhraseDraft = "";
let newSpokenTextDraft = "";
let newSpokenTextTouched = false;
let previewAsset = "";
let previewFallbackAsset = "";
let cleanupBusy = false;
let cleanupPreview = null;

function cleanupBlocked() {
  return state.soundbankSaving || Object.keys(state.patch).length > 0
    || activeGenerations.size > 0
    || activeOptimizations.size > 0
    || generationDirectoryLocked();
}

export function soundbankAuthoringBusy() {
  return cleanupBusy || activeGenerations.size > 0 || activeOptimizations.size > 0;
}

function rememberRetired(entry, includeCanonical = true) {
  if (includeCanonical && entryFile(entry)) state.soundbankRetiredDrafts.add(entryFile(entry));
  const optimized = entryOptimized(entry);
  if (optimized?.file) state.soundbankRetiredDrafts.add(optimized.file);
}

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

function entryText(entry, fallbackPhrase) {
  if (entry && typeof entry === "object" && !Array.isArray(entry)) {
    const text = entry.text;
    if (typeof text === "string" && text.trim()) return text.trim();
  }
  return fallbackPhrase;
}

function entryProvenance(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const provenance = entry.generated_by;
  return provenance && typeof provenance === "object" && !Array.isArray(provenance)
    ? provenance
    : null;
}

function entryOptimized(entry) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const optimized = entry.optimized;
  return optimized && typeof optimized === "object" && !Array.isArray(optimized)
    ? optimized
    : null;
}

function hasOptimized(entry) {
  return Boolean(
    entry
    && typeof entry === "object"
    && !Array.isArray(entry)
    && Object.prototype.hasOwnProperty.call(entry, "optimized"),
  );
}

function optimizedApplicable(entry) {
  const optimized = entryOptimized(entry);
  const runtimeAudio = state.startupSoundbankAudio || {};
  return Boolean(
    optimized
    && typeof optimized.file === "string"
    && /\.p3$/i.test(optimized.file)
    && optimized.codec === runtimeAudio.codec
    && Number.isInteger(optimized.sample_rate)
    && optimized.sample_rate === runtimeAudio.sample_rate
    && Number.isInteger(optimized.channels)
    && optimized.channels === runtimeAudio.channels
    && Number.isInteger(optimized.frame_duration_ms)
    && optimized.frame_duration_ms === runtimeAudio.frame_duration_ms
    && optimized.frame_duration_ms === P3_FRAME_DURATION_MS,
  );
}

function previewFile(entry) {
  const optimized = entryOptimized(entry);
  return optimizedApplicable(entry) ? optimized.file : entryFile(entry);
}

function optimizationMarkup(entry) {
  const optimized = entryOptimized(entry);
  const canonicalFile = entryFile(entry);
  if (optimizedApplicable(entry)) {
    const sampleRateKhz = Number(optimized.sample_rate) / 1000;
    return `<div class="soundbank-optimization ready">P3 optimized · Opus ${escapeHtml(sampleRateKhz)} kHz · ${escapeHtml(optimized.frame_duration_ms)} ms</div>`;
  }
  if (hasOptimized(entry)) {
    return '<div class="soundbank-optimization stale">P3 optimization is stale or incompatible</div>';
  }
  if (/\.p3$/i.test(canonicalFile)) {
    return '<div class="soundbank-optimization ready">P3 direct · prevalidated at server startup</div>';
  }
  return '<div class="soundbank-optimization">Canonical asset · P3 optimization available</div>';
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

function isSoundbankEmoji(character) {
  const codePoint = character.codePointAt(0);
  return EMOJI_SEQUENCE_CODE_POINTS.has(codePoint)
    || EMOJI_CODE_POINT_RANGES.some(
      ([start, end]) => codePoint >= start && codePoint <= end,
    );
}

function isSoundbankEdgeSeparator(character) {
  return /\s/u.test(character) || /\p{P}/u.test(character) || isSoundbankEmoji(character);
}

function normalizeSoundbankPhrase(phrase) {
  const characters = Array.from(String(phrase || "").normalize("NFC"));
  let start = 0;
  let end = characters.length;
  while (start < end && isSoundbankEdgeSeparator(characters[start])) start += 1;
  while (end > start && isSoundbankEdgeSeparator(characters[end - 1])) end -= 1;
  return characters.slice(start, end).join("").replace(/\s+/gu, " ").trim().toLowerCase();
}

function phraseError(phrase, originalKey = "") {
  const normalizedPhrase = normalizeSoundbankPhrase(phrase);
  if (!normalizedPhrase) return "Phrase must contain matchable text.";
  const duplicate = Object.keys(entries()).some(
    (candidate) => candidate !== originalKey
      && normalizeSoundbankPhrase(candidate) === normalizedPhrase,
  );
  return duplicate ? "Phrase duplicates another entry after runtime normalization." : "";
}

function spokenTextError(text) {
  if (!String(text || "").trim()) return "Spoken text is required for generation.";
  if (String(text).trim().length > 512) return "Spoken text must not exceed 512 characters.";
  return "";
}

function normalizedDirectory(directory) {
  return String(directory || "data/soundbank")
    .trim()
    .replaceAll("\\", "/")
    .replace(/\/+$/, "");
}

function generationDirectoryLocked() {
  const currentDirectory = getPath(
    state.config,
    "static_soundbank.directory",
    "data/soundbank",
  );
  return normalizedDirectory(currentDirectory)
    !== normalizedDirectory(state.startupSoundbankDirectory);
}

function generationStatus() {
  if (generationDirectoryLocked()) {
    return { label: "Save + restart before generating", className: "attention" };
  }
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
  const model = provenance.model ?? settings.model ?? settings.model_name ?? settings.model_repo;
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

function generationButton(label, action, key, disabled = false) {
  const loading = activeGenerations.has(key);
  return `
    <button class="button secondary${loading ? " loading" : ""}" data-soundbank-action="${action}" type="button" ${disabled || loading || cleanupBusy || state.soundbankSaving ? "disabled" : ""}>
      <span class="soundbank-button-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(loading ? "Generating…" : label)}</span>
    </button>`;
}

function optimizationButton(entry, key, disabled = false) {
  const canonicalFile = entryFile(entry);
  if (/\.p3$/i.test(canonicalFile)) return "";
  const loading = activeOptimizations.has(key);
  const label = hasOptimized(entry) ? "Re-optimize" : "Optimize";
  return `
    <button class="button secondary${loading ? " loading" : ""}" data-soundbank-action="optimize" type="button" ${disabled || loading ? "disabled" : ""}>
      <span class="soundbank-button-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(loading ? "Optimizing…" : label)}</span>
    </button>`;
}

function entryCard(phrase, entry, index) {
  const file = entryFile(entry);
  const spokenText = entryText(entry, phrase);
  const explicitText = Boolean(
    entry
    && typeof entry === "object"
    && !Array.isArray(entry)
    && typeof entry.text === "string"
    && entry.text.trim(),
  );
  const busy = activeGenerations.has(phrase) || activeOptimizations.has(phrase) || cleanupBusy || state.soundbankSaving;
  const error = generationErrors.get(phrase) || "";
  const invalid = Boolean(phraseError(phrase, phrase) || assetError(file));
  const generationLocked = generationDirectoryLocked();
  const rowId = `soundbank-entry-${index}`;
  return `
    <article class="soundbank-entry-card${busy ? " generating" : ""}" data-soundbank-entry data-original-key="${escapeHtml(phrase)}">
      <div class="soundbank-entry-fields">
        <label class="soundbank-entry-phrase" for="${rowId}-phrase">
          <span>Match phrase</span>
          <input id="${rowId}-phrase" data-soundbank-phrase value="${escapeHtml(phrase)}" ${busy ? "disabled" : ""} />
        </label>
        <label class="soundbank-entry-spoken" for="${rowId}-text">
          <span>Spoken text / subtitle</span>
          <input id="${rowId}-text" data-soundbank-text data-explicit-text="${explicitText}" data-spoken-dirty="false" value="${escapeHtml(spokenText)}" maxlength="512" ${busy ? "disabled" : ""} />
        </label>
        <label for="${rowId}-file">
          <span>Asset filename</span>
          <input id="${rowId}-file" data-soundbank-file value="${escapeHtml(file)}" spellcheck="false" ${busy ? "disabled" : ""} />
        </label>
      </div>
      <p class="soundbank-spoken-note" data-soundbank-spoken-note>Generate with current TTS to apply this spoken-text change.</p>
      <footer class="soundbank-entry-footer">
        <div class="soundbank-entry-metadata">
          ${provenanceMarkup(entry)}
          ${optimizationMarkup(entry)}
        </div>
        <div class="soundbank-entry-actions">
          ${generationButton("Generate with current TTS", "generate-current", phrase, generationLocked || invalid || busy)}
          <button class="button secondary" data-soundbank-action="preview" type="button" ${busy || invalid ? "disabled" : ""}>Preview</button>
          ${optimizationButton(entry, phrase, generationLocked || invalid || busy)}
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
  const newBusy = activeGenerations.has(NEW_ENTRY_KEY) || cleanupBusy || state.soundbankSaving;
  const newError = generationErrors.get(NEW_ENTRY_KEY) || "";
  const generationLocked = generationDirectoryLocked();
  const newPhraseValidation = newPhraseDraft.trim()
    ? phraseError(newPhraseDraft.trim())
    : "";
  const newSpokenValidation = newSpokenTextDraft.trim()
    ? spokenTextError(newSpokenTextDraft)
    : "";
  const newDisabled = generationLocked
    || !newPhraseDraft.trim()
    || !newSpokenTextDraft.trim()
    || Boolean(newPhraseValidation)
    || Boolean(newSpokenValidation)
    || newBusy;

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
            <input id="soundbankDirectory" value="${escapeHtml(directory)}" placeholder="data/soundbank" spellcheck="false" ${soundbankAuthoringBusy() || state.soundbankSaving ? "disabled" : ""} />
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
            <p class="field-help${generationLocked ? " soundbank-warning-text" : ""}">${generationLocked ? "Soundbank directory differs from the running server. Save and restart before generating." : "Save and restart provider changes before generating with the newly configured TTS."}</p>
          </div>
        </div>
        <form id="soundbankNewForm" class="soundbank-new-form">
          <label for="soundbankNewPhrase">
            <span>Match phrase</span>
            <input id="soundbankNewPhrase" value="${escapeHtml(newPhraseDraft)}" placeholder="What should trigger this audio?" maxlength="512" required ${newBusy ? "disabled" : ""} />
          </label>
          <label for="soundbankNewText">
            <span>Spoken text / subtitle</span>
            <input id="soundbankNewText" value="${escapeHtml(newSpokenTextDraft)}" placeholder="What should the robot say?" maxlength="512" required ${newBusy ? "disabled" : ""} />
          </label>
          ${generationButton("Generate with current TTS", "generate-new", NEW_ENTRY_KEY, newDisabled)}
          <p class="soundbank-row-error${newError || newPhraseValidation || newSpokenValidation ? " visible" : ""}" id="soundbankNewError" aria-live="polite">${escapeHtml(newError || newPhraseValidation || newSpokenValidation)}</p>
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
            <p class="field-help">Match phrase and asset edits update local Settings state. Spoken-text edits are applied only by generation so metadata cannot drift from the audio. Paths must be relative, exclude <code>..</code>, and use <code>.p3</code>, <code>.wav</code>, or <code>.mp3</code>.</p>
          </div>
          <span class="badge">${entryList.length} ${entryList.length === 1 ? "entry" : "entries"}</span>
        </div>
        <div class="soundbank-entry-list" id="soundbankEntries">
          ${entryList.length
            ? entryList.map(([phrase, entry], index) => entryCard(phrase, entry, index)).join("")
            : '<p class="soundbank-empty">No static soundbank entries configured.</p>'}
        </div>
      </section>

      <section class="settings-group panel soundbank-cleanup-panel">
        <header>
          <div>
            <h3>Unused sounds</h3>
            <p>Retired generated audio is cleaned after Save, or after restart when still used by the running server.</p>
          </div>
        </header>
        <div class="soundbank-cleanup-body">
          <p class="field-help">Preview unused WAV, MP3 and P3 files in this directory, including manually added audio. Saved entries, runtime entries and generated drafts are kept. Save or discard edits before cleanup. Unsaved generated drafts remain protected until saved or the server restarts.</p>
          <button id="soundbankCleanupPreview" class="button secondary" type="button" ${cleanupBusy || cleanupBlocked() ? "disabled" : ""}>${cleanupBusy ? "Working…" : "Preview unused sounds"}</button>
          ${cleanupPreview && !cleanupBlocked() ? `
            <p class="soundbank-cleanup-summary" role="status">${cleanupPreview.count} unused ${cleanupPreview.count === 1 ? "file" : "files"} · ${escapeHtml(formatBytes(cleanupPreview.bytes))}</p>
            ${cleanupPreview.count ? `
              <details class="soundbank-cleanup-files"><summary>Files to delete</summary><ul>${cleanupPreview.files.map((file) => `<li><code>${escapeHtml(file)}</code></li>`).join("")}</ul></details>
              <button id="soundbankCleanupDelete" class="button secondary" type="button" ${cleanupBusy ? "disabled" : ""}>Delete ${cleanupPreview.count} unused files</button>` : ""}
          ` : ""}
        </div>
      </section>
    </div>`;
}

async function cleanupSounds(action) {
  if (cleanupBusy || cleanupBlocked()) return;
  const token = cleanupPreview?.token;
  if (action === "delete" && !token) return;
  cleanupBusy = true;
  renderSoundbank();
  try {
    const response = await fetch("/api/settings/soundbank/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...(action === "delete" ? { token } : {}) }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Soundbank cleanup failed");
    if (action === "preview") {
      cleanupPreview = payload;
    } else {
      cleanupPreview = null;
      toast(`Deleted ${payload.deleted} unused files · ${formatBytes(payload.bytes)} freed.`);
      if (payload.errors?.length) toast(`Some files could not be deleted: ${payload.errors.join("; ")}`, true);
    }
  } catch (error) {
    cleanupPreview = null;
    toast(error.message, true);
  } finally {
    cleanupBusy = false;
    renderSoundbank();
  }
}

function validateRow(row) {
  const originalKey = row.dataset.originalKey;
  const phraseInput = row.querySelector("[data-soundbank-phrase]");
  const textInput = row.querySelector("[data-soundbank-text]");
  const fileInput = row.querySelector("[data-soundbank-file]");
  const errorElement = row.querySelector("[data-soundbank-error]");
  const phrase = phraseInput.value.trim();
  const spokenText = textInput.value.trim();
  const file = fileInput.value.trim();
  const phraseMessage = phraseError(phrase, originalKey);
  const spokenMessage = phraseMessage ? "" : spokenTextError(spokenText);
  const fileMessage = phraseMessage || spokenMessage ? "" : assetError(file);
  const message = phraseMessage || spokenMessage || fileMessage;

  phraseInput.setCustomValidity(phraseMessage);
  textInput.setCustomValidity(spokenMessage);
  fileInput.setCustomValidity(fileMessage);
  errorElement.textContent = message;
  errorElement.classList.toggle("visible", Boolean(message));
  row.classList.toggle("invalid", Boolean(message));

  const originalEntry = entries()[originalKey];
  const fileChanged = file !== entryFile(originalEntry).trim();
  const effectiveStoredText = entryText(originalEntry, phrase);
  const spokenChanged = spokenText !== effectiveStoredText;
  textInput.dataset.spokenDirty = String(spokenChanged);
  row.querySelector("[data-soundbank-spoken-note]")?.classList.toggle("visible", spokenChanged);
  const generationLocked = generationDirectoryLocked();
  row.querySelectorAll('[data-soundbank-action="generate-current"]').forEach((button) => {
    button.disabled = generationLocked || Boolean(message);
  });
  row.querySelectorAll('[data-soundbank-action="preview"]').forEach((button) => {
    button.disabled = Boolean(message);
  });
  row.querySelectorAll('[data-soundbank-action="optimize"]').forEach((button) => {
    button.disabled = generationLocked || Boolean(message) || !/\.(?:wav|mp3)$/i.test(file);
  });
  return {
    valid: !message,
    phrase,
    spokenText,
    file,
    originalKey,
    originalEntry,
  };
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
    return {
      phrase: result.phrase,
      spokenText: result.spokenText,
      entry: result.originalEntry,
    };
  }
  if (result.originalEntry && typeof result.originalEntry === "object" && !Array.isArray(result.originalEntry)) {
    if (fileChanged) {
      updatedEntry = { ...result.originalEntry, file: result.file };
      delete updatedEntry.generated_by;
      delete updatedEntry.optimized;
      delete updatedEntry.text;
    }
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
  if (fileChanged && row.querySelector("[data-soundbank-text]").dataset.spokenDirty !== "true") {
    row.querySelector("[data-soundbank-text]").value = result.phrase;
    row.querySelector("[data-soundbank-text]").dataset.explicitText = "false";
  }
  refreshApplyStatus();

  if (fileChanged) {
    row.querySelector(".soundbank-provenance").outerHTML = provenanceMarkup(updatedEntry);
    rememberRetired(result.originalEntry);
    row.querySelector(".soundbank-optimization").outerHTML = optimizationMarkup(updatedEntry);
  }
  const validated = validateRow(row);
  return {
    phrase: result.phrase,
    spokenText: validated.spokenText,
    entry: updatedEntry,
  };
}

function removeEntry(phrase) {
  const removedEntry = entries()[phrase];
  rememberRetired(removedEntry);
  const updatedEntries = {};
  Object.entries(entries()).forEach(([candidate, entry]) => {
    if (candidate !== phrase) updatedEntries[candidate] = entry;
  });
  if (
    previewAsset === entryFile(removedEntry)
    || previewAsset === previewFile(removedEntry)
  ) {
    previewAsset = "";
    previewFallbackAsset = "";
  }
  generationErrors.delete(phrase);
  updateValue("static_soundbank.entries", updatedEntries);
  renderSoundbank();
}

async function generateEntry(key, title, text) {
  if (generationDirectoryLocked()) {
    generationErrors.set(
      key,
      "Save and restart the server before generating in the new soundbank directory.",
    );
    renderSoundbank();
    return;
  }
  if (activeGenerations.has(key)) return;
  activeGenerations.set(key, true);
  generationErrors.delete(key);
  renderSoundbank();

  try {
    const response = await fetch("/api/settings/soundbank/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        text,
        soundbank_draft_id: state.soundbankDraftId,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Soundbank generation failed");
    if (!payload.entry || typeof payload.entry !== "object" || Array.isArray(payload.entry)) {
      throw new Error("Soundbank generation returned an invalid entry");
    }
    if (generationDirectoryLocked()) {
      throw new Error(
        "Soundbank directory changed while generating; the generated entry was not added.",
      );
    }

    const updatedEntries = {};
    rememberRetired(entries()[key === NEW_ENTRY_KEY ? title : key]);
    if (key === NEW_ENTRY_KEY) {
      Object.assign(updatedEntries, entries(), { [title]: payload.entry });
      newPhraseDraft = "";
      newSpokenTextDraft = "";
      newSpokenTextTouched = false;
    } else {
      Object.entries(entries()).forEach(([candidate, entry]) => {
        updatedEntries[candidate] = candidate === key ? payload.entry : entry;
      });
    }
    updateValue("static_soundbank.entries", updatedEntries);
    toast(`Generated soundbank audio for “${title}”. Save changes to persist the entry.`);
  } catch (error) {
    generationErrors.set(key, error.message || "Soundbank generation failed");
  } finally {
    activeGenerations.delete(key);
    renderSoundbank();
  }
}

async function optimizeEntry(key, entry) {
  if (generationDirectoryLocked()) {
    generationErrors.set(
      key,
      "Save and restart the server before optimizing in the new soundbank directory.",
    );
    renderSoundbank();
    return;
  }
  if (activeOptimizations.has(key)) return;
  activeOptimizations.add(key);
  generationErrors.delete(key);
  renderSoundbank();

  try {
    const response = await fetch("/api/settings/soundbank/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: entryFile(entry), soundbank_draft_id: state.soundbankDraftId }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Soundbank optimization failed");
    if (!payload.optimized || typeof payload.optimized !== "object" || Array.isArray(payload.optimized)) {
      throw new Error("Soundbank optimization returned invalid metadata");
    }
    if (generationDirectoryLocked()) {
      throw new Error(
        "Soundbank directory changed while optimizing; the optimized asset was not added.",
      );
    }

    const updatedEntries = {};
    Object.entries(entries()).forEach(([candidate, currentEntry]) => {
      if (candidate !== key) {
        updatedEntries[candidate] = currentEntry;
        return;
      }
      rememberRetired(currentEntry, false);
      updatedEntries[candidate] = currentEntry && typeof currentEntry === "object" && !Array.isArray(currentEntry)
        ? { ...currentEntry, optimized: payload.optimized }
        : { file: entryFile(currentEntry), optimized: payload.optimized };
    });
    updateValue("static_soundbank.entries", updatedEntries);
    toast(`Optimized soundbank audio for “${key}”. Save changes to persist the metadata.`);
  } catch (error) {
    generationErrors.set(key, error.message || "Soundbank optimization failed");
  } finally {
    activeOptimizations.delete(key);
    renderSoundbank();
  }
}

function attachRowListeners(row) {
  const phraseInput = row.querySelector("[data-soundbank-phrase]");
  const textInput = row.querySelector("[data-soundbank-text]");
  const fileInput = row.querySelector("[data-soundbank-file]");

  phraseInput.addEventListener("input", () => {
    generationErrors.delete(row.dataset.originalKey);
    if (
      textInput.dataset.explicitText !== "true"
      && textInput.dataset.spokenDirty !== "true"
    ) {
      textInput.value = phraseInput.value;
    }
    validateRow(row);
  });
  phraseInput.addEventListener("change", () => commitRow(row));

  textInput.addEventListener("input", () => {
    generationErrors.delete(row.dataset.originalKey);
    textInput.dataset.spokenDirty = "true";
    validateRow(row);
  });

  fileInput.addEventListener("input", () => {
    generationErrors.delete(row.dataset.originalKey);
    validateRow(row);
  });
  fileInput.addEventListener("change", () => commitRow(row));

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
        previewAsset = previewFile(committed.entry);
        previewFallbackAsset = previewAsset !== entryFile(committed.entry)
          ? entryFile(committed.entry)
          : "";
        renderSoundbank();
        const audio = $("#soundbankAudio");
        audio?.play().catch(() => {
          const status = $("#soundbankPreviewStatus");
          if (status) status.textContent = "Preview is ready; press play to start audio.";
        });
      } else if (action === "generate-current") {
        generateEntry(
          committed.phrase,
          committed.phrase,
          committed.spokenText,
        );
      } else if (action === "optimize") {
        optimizeEntry(committed.phrase, committed.entry);
      }
    });
  });
}

function attachListeners() {
  $("#soundbankCleanupPreview")?.addEventListener("click", () => cleanupSounds("preview"));
  $("#soundbankCleanupDelete")?.addEventListener("click", () => cleanupSounds("delete"));
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
  const newTextInput = $("#soundbankNewText");
  const refreshNewEntryValidation = () => {
    const phraseMessage = newPhraseDraft.trim()
      ? phraseError(newPhraseDraft.trim())
      : "";
    const spokenMessage = newSpokenTextDraft.trim()
      ? spokenTextError(newSpokenTextDraft)
      : "";
    const message = phraseMessage || spokenMessage;
    newInput.setCustomValidity(phraseMessage);
    newTextInput.setCustomValidity(spokenMessage);
    $("#soundbankNewError").textContent = message;
    $("#soundbankNewError").classList.toggle("visible", Boolean(message));
    const button = $('#soundbankNewForm [data-soundbank-action="generate-new"]');
    if (button) {
      button.disabled = generationDirectoryLocked()
        || !newPhraseDraft.trim()
        || !newSpokenTextDraft.trim()
        || Boolean(message);
    }
  };
  newInput?.addEventListener("input", () => {
    newPhraseDraft = newInput.value;
    if (!newSpokenTextTouched) {
      newSpokenTextDraft = newPhraseDraft;
      newTextInput.value = newSpokenTextDraft;
    }
    generationErrors.delete(NEW_ENTRY_KEY);
    refreshNewEntryValidation();
  });
  newTextInput?.addEventListener("input", () => {
    newSpokenTextDraft = newTextInput.value;
    newSpokenTextTouched = newSpokenTextDraft !== newPhraseDraft;
    generationErrors.delete(NEW_ENTRY_KEY);
    refreshNewEntryValidation();
  });
  $('#soundbankNewForm [data-soundbank-action="generate-new"]')?.addEventListener("click", () => {
    $("#soundbankNewForm")?.requestSubmit();
  });
  $("#soundbankNewForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const phrase = newPhraseDraft.trim();
    const spokenText = newSpokenTextDraft.trim();
    const message = phraseError(phrase) || spokenTextError(spokenText);
    if (message) {
      refreshNewEntryValidation();
      $("#soundbankNewForm")?.querySelector(":invalid")?.reportValidity();
      return;
    }
    generateEntry(NEW_ENTRY_KEY, phrase, spokenText);
  });

  const audio = $("#soundbankAudio");
  audio?.addEventListener("error", () => {
    if (previewFallbackAsset) {
      previewAsset = previewFallbackAsset;
      previewFallbackAsset = "";
      renderSoundbank();
      const fallbackStatus = $("#soundbankPreviewStatus");
      if (fallbackStatus) {
        fallbackStatus.textContent = "Optimized preview was unavailable; using the canonical asset.";
      }
      $("#soundbankAudio")?.play().catch(() => {});
      return;
    }
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
