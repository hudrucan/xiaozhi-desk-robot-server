# First-run Cloud Restore Wizard V1

A fresh clone can start without local provider configuration. Run `python app.py`
from `main/xiaozhi-server` after installing the server dependencies; open
`http://localhost:8003/setup/`. This restores an existing recoverable logical node.
It does not create a source/node or perform provisioning.

## Browser flow

1. Drag/drop or choose the Google **Desktop app** OAuth JSON belonging to the
   original source. The file is limited to 32 KB, validated and installed privately.
2. Click **Authorize Google**. Login opens in a new tab. Leave the setup page open;
   it polls for completion and saves private authorized-user credentials.
3. Choose the Cloud State source and node. One source selects automatically;
   an exact normalized-hostname node match wins, otherwise a single node selects
   automatically. Multiple unmatched nodes require an explicit selection.
4. Enter the recovery passphrase and click **Restore & Activate**. The password
   input clears on submission; neither passwords nor tokens are rendered back.
5. After Config, Soundbank, Memory and Secrets show OK, click **Restart server**.
   Normal Cloud startup then performs the existing materialization and active-LKG
   flow. Setup routes are absent in normal mode.

The Google application must still be registered once; use the original client
for existing Cloud State. See [OAuth client setup](google-drive-oauth-setup.md).

## Headless SSH

Setup binds only to `127.0.0.1`; it has no LAN option. On the laptop opening the
browser, run the two-port command printed at startup (substitute your SSH target):

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 8003:127.0.0.1:8003 \
  -L 8765:127.0.0.1:8765 \
  user@host
```

Open `http://localhost:8003/setup/` on that laptop. Google redirects to
`127.0.0.1:8765` on the same laptop and the tunnel carries its callback to the
server. No terminal URL/code copy is required. Login expires after 600 seconds;
retry from the wizard for a fresh session. Keep the tunnel open until setup ends.
If the reference configuration changes the default HTTP port, startup prints the
corresponding port and tunnel. Browser OAuth remains fixed at 8765.

## Ownership and failure invariants

`config/first_run.py` seeds only when bootstrap, Local override and config.d are
all absent, and no other installation state exists. Pre-installed OAuth client
and credentials are permitted. Existing Local installs, Cloud bootstrap, broken
config files/directories and recovery state never trigger a silent reset. Symlink
state is rejected or left to the existing normal validation path.

The private `data/.first-run` marker is durable before the `{}` Local seed is
written, so a crash cannot cause that seed to be mistaken for a legacy install.
Both files are mode 0600 and `data/` is 0700. A surviving marker always resumes
setup, including a crash during initial seeding; other files are not repaired.

`app.py` branches before importing normal runtime servers or initializing
logging/config. Setup runs a separate HTTP application: no WebSocket, provider,
Memory runtime, soundbank service, OTA, MCP or Vision initialization occurs.
The normal startup path is retained, including mark_applied after both listeners.

`OAuthLoopbackSession` owns one process-wide session. It uses the installed-app
flow with PKCE and the existing `drive.file` scope, validates state before code
exchange, and runs a quiet IPv4 callback worker. State/verifier/code stay in
process memory. Completion, timeout, failed start and shutdown release ownership
and close the listener; token exchange has a bounded network timeout. UI and CLI
use the same primitive. CLI restore still keeps new credentials in memory until
its journal publishes them; the wizard explicitly saves credentials after login.
Uploads and credentials use create-only storage and cannot replace a different
application/account file. No raw provider exception text is returned or logged.

Setup HTTP requires a loopback peer, a loopback Host, same browser Origin and a
custom header on mutations. It enables no CORS, disables response caching and
access logs, bounds the entire multipart body, accepts only a single file, and
never uses the supplied filename as a filesystem path. Discovery returns only
source UUID/label/node names. Mutation requests are serialized, and workers remain
owned through client disconnect or server shutdown.

Restore rediscovers descriptors on every submit. It delegates source/node
selection, in-memory decryption, Config/Soundbank/Memory readiness, ETag recheck
and local publication to `restore_cloud_node(..., activate=True)`. The recovery
journal still publishes credentials, node secrets, then bootstrap **last**.
Only its full successful return permits marker removal and directory fsync.
Wrong passphrases, preflight failures or conflicts leave setup pending. A browser
disconnect after successful publication can reload the finished screen.
Restart uses the existing delayed event plus graceful cleanup and `os.execv`.

No firmware, Cloud State schema, manifest, encryption/AAD or provisioning format
changes are part of this wizard. Existing CLI setup and recovery remain available.

## Validation and manual smoke test

Regression tests use fake OAuth callback servers and fake Drive. API tests use a
temporary localhost HTTP test application, never the actual robot server. Run
from `main/xiaozhi-server` when authorized:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Manual validation still requires a disposable fresh checkout and a recoverable
test source. Start the server and confirm setup-only output, private seed files
and no WebSocket listener. Through the two-port tunnel, upload the original
Desktop JSON, login and select the source/node. Try a wrong recovery passphrase:
the marker and absence of bootstrap must remain. Then restore correctly, verify
the success summary and Google Drive bootstrap, restart, and confirm normal Cloud
startup plus `/setup/` and `/api/setup/status` returning 404. An existing Local
installation must start normally without a wizard. No hardware/flash test is needed.
