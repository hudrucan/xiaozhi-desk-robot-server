const $ = (id) => document.getElementById(id);
let sources = [], hostname = '', pollTimer = null, cloneIntent = null;
let initializing = true, oauthActivity = null;
const activities = new Map();

function isBusy() { return initializing || activities.size > 0; }
function renderBusy() {
  const busy = isBusy();
  $('wizard').hidden = initializing;
  $('wizard').disabled = busy;
  $('wizard').inert = busy;
  $('wizard').setAttribute('aria-busy', String(busy));
  $('loading').hidden = !busy;
  $('loading-text').textContent = Array.from(activities.values()).pop() || 'Checking setup progress…';
  if (busy) $('drop-zone').classList.remove('dragging');
}
function activity(text) {
  const key = Symbol();
  activities.set(key, text);
  renderBusy();
  return {
    update(label) { activities.set(key, label); renderBusy(); },
    finish() { activities.delete(key); renderBusy(); }
  };
}
async function whileBusy(text, work) {
  const pending = activity(text);
  try { return await work(); }
  finally { pending.finish(); }
}
function finishOAuth() { oauthActivity?.finish(); oauthActivity = null; }

function message(text, error = false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
}
function step(name) {
  for (const item of ['google', 'cloud', 'recovery', 'finished']) {
    $(`${item}-panel`).hidden = item !== name;
    $(`step-${item}`).removeAttribute('aria-current');
  }
  $(`step-${name}`).setAttribute('aria-current', 'step');
}
async function api(path, {body, ...options} = {}) {
  const headers = {'X-Setup-Request': '1'};
  if (body && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const response = await fetch(`/api/setup/${path}`, {cache: 'no-store', credentials: 'same-origin', ...options, headers, body});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Setup unavailable. Retry this step.');
  return result;
}
function option(select, value, label) {
  const item = document.createElement('option');
  item.value = value;
  item.textContent = label;
  select.append(item);
}
function chooseNodes() {
  const source = sources.find(item => item.source_id === $('source').value);
  $('node').replaceChildren();
  const nodes = source?.nodes || [];
  const selected = cloneIntent && source?.source_id === cloneIntent.source_id ? cloneIntent.source_node_id
    : nodes.includes(hostname) ? hostname : nodes.length === 1 ? nodes[0] : '';
  if (!selected) option($('node'), '', 'Choose an existing node');
  for (const node of nodes) option($('node'), node, node);
  $('node').value = selected;
  updateMode();
}
function updateMode() {
  const clone = $('restore-mode').value === 'clone';
  $('new-node-block').hidden = !clone;
  $('clone-warning').hidden = !clone;
  $('node-label').textContent = clone ? 'Source backup node' : 'Node';
  $('continue').textContent = clone ? 'Continue to clone' : 'Continue to recovery';
  $('restore').textContent = clone ? 'Create node & Activate' : 'Restore & Activate';
  const source = sources.find(item => item.source_id === $('source').value);
  const newId = $('new-node-id').value;
  const resuming = cloneIntent && cloneIntent.source_id === source?.source_id && cloneIntent.new_node_id === newId;
  const exists = source?.nodes.includes(newId) && !resuming;
  const valid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$/.test(newId);
  $('new-node-hint').textContent = exists ? 'This ID already exists. Choose a new node ID.'
    : !valid ? 'Use letters, digits, dots, underscores or hyphens, starting with a letter or digit.'
    : resuming ? 'Continuing the saved clone transaction. Source and new ID are fixed until it completes.'
    : 'The new ID must be absent from both Cloud Config and the recovery descriptor.';
  $('continue').disabled = !source || !$('node').value || (clone && (!valid || exists));
  for (const id of ['source', 'node', 'restore-mode', 'new-node-id']) $(id).disabled = Boolean(cloneIntent);
}
async function loadSources() {
  await whileBusy('Loading Cloud State sources…', async () => {
    const result = await api('sources');
    sources = result.sources;
    hostname = result.hostname;
    $('source').replaceChildren();
    if (sources.length !== 1) option($('source'), '', sources.length ? 'Choose a Cloud State source' : 'No recoverable Cloud State found');
    for (const source of sources) option($('source'), source.source_id, source.label);
    if (result.selected_source_id) $('source').value = result.selected_source_id;
    if (cloneIntent) {
      $('source').value = cloneIntent.source_id;
      $('restore-mode').value = 'clone';
    }
    if (!$('new-node-id').value) $('new-node-id').value = cloneIntent?.new_node_id || hostname;
    chooseNodes();
    step('cloud');
    message(sources.length ? 'Google connected. Choose your Cloud State.' : 'No sources found. Use the original OAuth client and Google account; the source needs a recovery backup.');
  });
}
async function upload(file) {
  if (isBusy() || !file) return;
  if (file.size > 32 * 1024) { message('OAuth JSON must be at most 32 KB.', true); return; }
  const pending = activity('Validating OAuth client…');
  $('authorize').disabled = true;
  const body = new FormData();
  body.append('file', file);
  message('Validating OAuth client…');
  try {
    await api('oauth-client', {method: 'POST', body});
    $('client-status').textContent = 'Desktop OAuth client installed.';
    $('authorize').disabled = false;
    message('Client ready. Authorize Google next.');
  } catch (error) {
    message(error.message, true);
    const status = await api('status').catch(() => null);
    $('authorize').disabled = !status?.client_installed;
  }
  finally { $('client-file').value = ''; pending.finish(); }
}
$('client-file').addEventListener('change', event => upload(event.target.files[0]));
const zone = $('drop-zone');
zone.addEventListener('keydown', event => { if (!isBusy() && ['Enter', ' '].includes(event.key)) { event.preventDefault(); $('client-file').click(); } });
for (const name of ['dragenter', 'dragover']) zone.addEventListener(name, event => { event.preventDefault(); if (!isBusy()) zone.classList.add('dragging'); });
for (const name of ['dragleave', 'drop']) zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove('dragging'); });
zone.addEventListener('drop', event => upload(event.dataTransfer.files[0]));
// Ignore file drops during loading, including outside the inert wizard.
for (const name of ['dragover', 'drop']) document.addEventListener(name, event => { if (isBusy()) event.preventDefault(); });

async function pollOAuth() {
  try {
    const result = await api('oauth/status');
    if (result.status === 'authorized') { await loadSources(); finishOAuth(); return; }
    if (result.status === 'failed') throw new Error(result.error || 'Login failed. Start a new authorization.');
    pollTimer = setTimeout(pollOAuth, 1000);
  } catch (error) { message(error.message, true); $('authorize').disabled = false; finishOAuth(); }
}
$('authorize').addEventListener('click', async () => {
  if (isBusy()) return;
  // Open synchronously during the click so popup protection does not block login.
  const popup = window.open('about:blank', '_blank');
  if (!popup) { message('Allow popups for this local setup page, then retry.', true); return; }
  popup.opener = null;
  oauthActivity = activity('Starting Google authorization…');
  $('authorize').disabled = true;
  clearTimeout(pollTimer);
  try {
    const result = await api('oauth/start', {method: 'POST'});
    if (result.status === 'authorized') { popup.close(); await loadSources(); finishOAuth(); return; }
    const url = new URL(result.authorization_url);
    if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') throw new Error('Invalid Google authorization URL.');
    popup.location.href = url.href;
    oauthActivity.update('Waiting for Google login…');
    message('Waiting for Google login. Keep your SSH tunnel open.');
    pollOAuth();
  } catch (error) { popup.close(); $('authorize').disabled = false; message(error.message, true); finishOAuth(); }
});
$('source').addEventListener('change', () => { if (!isBusy()) chooseNodes(); });
$('node').addEventListener('change', () => { if (!isBusy()) updateMode(); });
$('restore-mode').addEventListener('change', () => { if (!isBusy()) updateMode(); });
$('new-node-id').addEventListener('input', () => { if (!isBusy()) updateMode(); });
$('continue').addEventListener('click', () => {
  if (isBusy()) return;
  const source = sources.find(item => item.source_id === $('source').value);
  if (!source || !$('node').value) return;
  $('selection').textContent = `${source.label} · ${$('node').value}` +
    ($('restore-mode').value === 'clone' ? ` → new node ${$('new-node-id').value}` : '');
  step('recovery'); message(''); $('passphrase').focus();
});
$('back').addEventListener('click', () => { if (!isBusy()) { $('passphrase').value = ''; step('cloud'); } });
$('restore-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (isBusy()) return;
  const body = {source_id: $('source').value, node_id: $('node').value, passphrase: $('passphrase').value};
  const clone = $('restore-mode').value === 'clone';
  if (clone) { body.mode = 'clone'; body.new_node_id = $('new-node-id').value; }
  $('passphrase').value = '';
  const pending = activity(clone ? 'Creating and verifying the new Cloud node…' : 'Verifying and restoring Cloud State…');
  $('restore').disabled = $('back').disabled = true;
  message('Verifying and restoring Cloud State. Keep this page open…');
  try {
    await api('restore', {method: 'POST', body});
    $('finished-title').textContent = clone ? `New node ${body.new_node_id} activated` : 'Cloud State restored';
    step('finished'); message('Restore complete. Restart to apply Cloud State.');
  } catch (error) {
    message(error.message, true);
    if (clone) {
      const status = await api('status').catch(() => null);
      cloneIntent = status?.clone_selection || cloneIntent;
      updateMode();
    }
  }
  finally { body.passphrase = ''; $('restore').disabled = $('back').disabled = false; pending.finish(); }
});
$('restart').addEventListener('click', async () => {
  if (isBusy()) return;
  const pending = activity('Requesting server restart…');
  $('restart').disabled = true;
  try {
    await api('restart', {method: 'POST'});
    message('Restart requested. Open Settings once the server is ready.');
    $('settings-link').hidden = false;
  } catch (error) { message(error.message, true); $('restart').disabled = false; }
  finally { pending.finish(); }
});
window.addEventListener('pagehide', () => clearTimeout(pollTimer));
(async () => {
  try {
    const status = await api('status');
    hostname = status.hostname;
    cloneIntent = status.clone_selection;
    if (status.completed) { step('finished'); return; }
    $('authorize').disabled = !status.client_installed;
    $('client-status').textContent = status.client_installed ? 'Desktop OAuth client installed.' : 'No OAuth client installed.';
    if (status.credentials_installed) await loadSources();
    else {
      step('google');
      const oauth = await api('oauth/status');
      if (oauth.status === 'pending') {
        $('authorize').disabled = true;
        oauthActivity = activity('Waiting for Google login…');
        message('Waiting for Google login. Keep your SSH tunnel open.');
        pollOAuth();
      }
    }
  } catch (error) { step('google'); message(error.message, true); }
  finally { initializing = false; renderBusy(); }
})();
