# Google Drive OAuth setup

Register one Google Desktop OAuth client for the application and reuse it on
every box. Google login issues user tokens; it does not register an OAuth client.
To restore existing Cloud State, use the original application's client and the
Google account with access to that source. An unrelated client can fail discovery
with the narrow `drive.file` scope.

## One-time application registration

1. In [Google Cloud Console](https://console.cloud.google.com/), select/create a project.
2. Enable **Google Drive API** in **APIs & Services > Library**.
3. Configure **Google Auth Platform > Branding**, including app name and contact email.
4. For personal Gmail select **Audience > External**. If the app is Testing,
   add your Google account under **Test users**.
5. In **Data Access**, add `https://www.googleapis.com/auth/drive.file`.
6. In **Clients**, create a **Desktop app** client and download its JSON.

These are Google's application-registration prerequisites. See
[Desktop client setup](https://developers.google.com/workspace/drive/api/quickstart/python)
and [OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent).
No shared application credentials are shipped in this repository.

## Import and login

From the repository root, with server dependencies installed:

```bash
main/xiaozhi-server/.venv/bin/python scripts/setup_google_drive.py \
  --import-client /path/to/downloaded-client.json --authorize
```

There is no need to rename Google's downloaded file. The helper validates a
Desktop client and Google endpoints, installs `data/oauth-client.json`, and
creates `data/drive-credentials.json` after successful login. Both local files
are private (`0600`) and the `data` directory is ignored by Git. Setup never
overwrites a different client or token file, publishes Drive objects, changes
bootstrap, switches providers, or creates node identities. Repeating setup with
the same client reuses existing credentials; it does not verify their current
Google authorization. Runtime refreshes them when used. Revoked credentials
require explicit account/client review and removal of the old credentials before
authorizing again; changing clients can affect access to existing Cloud State.

Run the helper without arguments to print the registration steps. Use
`--import-client FILE` alone to install the client without logging in, or
`--authorize` alone to log in with the installed client.

## Headless server over SSH

On the server, from the repository root:

```bash
main/xiaozhi-server/.venv/bin/python scripts/setup_google_drive.py \
  --import-client /path/to/downloaded-client.json --authorize \
  --ssh-target robot@deskbox
```

SSH sessions and Linux without a display automatically avoid launching a
browser. `--no-browser` forces this behavior anywhere. The CLI prints this
ready-to-copy command for a **separate terminal on the laptop**:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 robot@deskbox
```

Keep the tunnel open and open the printed Google login link on that laptop.
The browser redirects to the laptop's IPv4 loopback, which SSH forwards to the
server. Neither the callback nor the token is pasted into the terminal.
The default port stays `8765` across retries; login waits up to 10 minutes.
Use `--oauth-port 8877` if either machine's port is occupied; the tunnel and
callback use the same chosen port. `--oauth-timeout 900` increases the wait.
Do not use another port on only one end or reuse an expired login link.
If your SSH connection needs a jump host, key, or custom port, add the same SSH
options to the printed command or use your configured SSH host alias.

Once authorized, provision as usual or restore without auth flags:

```bash
main/xiaozhi-server/.venv/bin/python scripts/restore_cloud_node.py --activate
```

Restore also accepts `--oauth-port`, `--oauth-timeout`, `--ssh-target`, and
`--no-browser` when a new login is needed. Its existing credential precedence
and explicit `--oauth-client` / `--credentials` behavior are preserved. Restore
continues to persist credentials only through its validated local transaction;
using the separate setup helper explicitly saves credentials before restore.

If login fails, the public error identifies client setup, missing dependencies,
occupied callback port, or incomplete authorization without printing private
provider exception text. Google still requires consent and a registered client;
SSH forwarding is required when a remote browser uses this loopback flow.

## Verification

Tests use mocked OAuth and fake Drive; real Google login requires manual validation.
From `main/xiaozhi-server`:

```bash
.venv/bin/python -m unittest discover -s tests -p test_cloud_recovery.py -q
```

Then perform one SSH login using a test account, check the two private files,
and rerun `--authorize` to confirm existing credentials are kept. Do not share
tokens or callback URLs containing an authorization code.
