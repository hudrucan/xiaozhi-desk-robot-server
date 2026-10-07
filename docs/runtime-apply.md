# Shared Settings runtime apply

Save remains a Cloud CAS publication. **Apply runtime** is a separate, opt-in
action for the already deployed three-node voice runtime. It applies the saved
ASR/VAD, Gemini LLM, Sherpa TTS and Soundbank bundles with existing installed
models. It does not initialize providers in the control plane, run Ansible/SSH,
change VIP assignment or claim unsupported MCP/runtime features are active.

The toolbar shows Apply runtime when a saved revision is not running on all
three nodes. The runtime panel reports actual targeted worker status and private
core status, rather than advancing the control plane's recorded `active_revision`.
Unsaved fields are never passed to the installer.

## Maintenance sequence

1. Confirm live shared V2 Cloud revision and prepare offline candidates on all
   nodes. Validate local keys, existing model checksums, ready Soundbank bytes
   and matching TTS voice identity before stopping any runtime.
2. Stop conversation cores and workers on all three nodes. Existing conversations
   disconnect; start a new conversation after completion. Settings, MQTT, NATS,
   HAProxy and Keepalived continue running.
3. Install private bundles atomically, start and verify each worker, then start
   cores only after every worker has the new revision. Native model warm-up can
   take several minutes across three nodes.
4. Verify all worker/core revisions and health before reporting completion.

Each mutation checks live Cloud revision again; a concurrent Save aborts apply.
Sorted root-journal acquisition prevents competing control planes from applying
different revisions. Any configured control plane can coordinate; no fixed leader.
HTTP polling obtains the active coordinator's job even when HAProxy selects a
different node. A disconnected browser does not cancel the owned background task.

## Recovery and security

Root-only journals retain the previous bundles and core revision overlay. Failed
apply stops all affected runtimes again, restores bundles, verifies old workers,
then reopens old cores. If a node is unreachable, cores remain paused until
**Recover runtime** can confirm restoration; it never silently opens cores against
an unknown mixture of revisions. Recovery restores the interrupted operation's
old revision even if a newer Cloud Save exists; Apply can then target the newer one.

Boot guards stop incomplete transactions from auto-starting after power loss.
Only short-lived root-created permits authorize local starts during apply/recovery.
The installer listens solely on `/run/xiaozhi-runtime-apply/apply.sock`, verifies
Linux peer UID, and accepts fixed metadata commands with bounded revision and
operation IDs. It executes only start/stop of the two fixed worker/core units.
There are no browser-provided commands, URLs, services, paths or package installs.
The unprivileged Settings process retains its systemd sandbox and has no sudo.

Private peer commands reuse the deployed authenticated-encryption peer map with
separate runtime action types and Cloud-authority binding. HAProxy must continue
blocking `/internal/`. Config bodies, keys and credential paths never cross NATS
or appear in public status. The installer uses validated local Cloud cache only;
Cloud refresh and Soundbank download remain owned by the control plane.

Deployment enables `XIAOZHI_RUNTIME_APPLY_ENABLED=true` only with all three peer
identities and the local root agent installed. Model/provider dependency changes
still require a reviewed Ansible asset/software deployment. Normal `app.py`
Settings restart behavior is unchanged.
