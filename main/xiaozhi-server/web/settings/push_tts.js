import { $, escapeHtml, state } from "./shared.js";

let sending = false;

function persistentConnections() {
  const items = state.resources?.runtime?.connections?.items || [];
  const byDevice = new Map();
  items.forEach((connection) => {
    if (connection.persistent_websocket && connection.device_id) {
      byDevice.set(connection.device_id, connection);
    }
  });
  return [...byDevice.values()];
}

export function renderPushTtsDevices() {
  const select = $("#pushTtsDevice");
  const button = $("#pushTtsButton");
  const status = $("#pushTtsStatus");
  if (!select || !button || !status) return;

  const previous = select.value;
  const connections = persistentConnections();
  select.innerHTML = connections.length
    ? connections.map((connection) => {
        const session = String(connection.session_id || "").slice(0, 8);
        const label = session
          ? `${connection.device_id} · ${session}`
          : connection.device_id;
        return `<option value="${escapeHtml(connection.device_id)}">${escapeHtml(label)}</option>`;
      }).join("")
    : '<option value="">No persistent-WebSocket robot connected</option>';

  if (connections.some((connection) => connection.device_id === previous)) {
    select.value = previous;
  }
  select.disabled = sending || !connections.length;
  button.disabled = sending || !connections.length;
  if (connections.length && !sending && !status.classList.contains("success") && !status.classList.contains("error")) {
    status.className = "push-tts-status muted";
    status.textContent = "Ready to generate and push. Playback is not confirmed by firmware.";
  } else if (!connections.length && !sending) {
    status.className = "push-tts-status muted";
    status.textContent = "Connect a persistent-WebSocket robot to enable Push TTS.";
  }
}

export function initializePushTts() {
  const form = $("#pushTtsForm");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending) return;

    const deviceId = $("#pushTtsDevice").value;
    const text = $("#pushTtsText").value.trim();
    const button = $("#pushTtsButton");
    const status = $("#pushTtsStatus");
    if (!deviceId || !text) {
      status.className = "push-tts-status error";
      status.textContent = "Choose a connected robot and enter text.";
      return;
    }

    sending = true;
    button.classList.add("loading");
    button.setAttribute("aria-label", "Generating audio");
    button.title = "Generating audio";
    status.className = "push-tts-status muted";
    status.textContent = "Generating Ogg/Opus and pushing notify…";
    renderPushTtsDevices();
    try {
      const response = await fetch("/api/settings/push-tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId, text }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "Push TTS failed");
      status.className = "push-tts-status success";
      status.textContent = payload.message ||
        "Audio generated and notify pushed; playback is not confirmed.";
    } catch (error) {
      status.className = "push-tts-status error";
      status.textContent = error.message;
    } finally {
      sending = false;
      button.classList.remove("loading");
      button.setAttribute("aria-label", "Send to robot");
      button.title = "Send to robot";
      renderPushTtsDevices();
    }
  });
}
