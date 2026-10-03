# Cloud State Recovery V1

Recovery reconstructs one logical node's local bootstrap, Google credentials and
exact named node-secret dataset. Config, Soundbank and Memory retain their existing
authorities and persistence protocols. It restores no arbitrary `data/` files and
starts no server. Firmware is unchanged.

## Prepare a recoverable source

With an existing app-owned Drive folder/Config manifest and Local deployment,
run from the repository root:

```bash
main/xiaozhi-server/.venv/bin/python scripts/provision_cloud_state.py \
  --from-local --source-label "Desk Robot"
```

The command asks for a recovery passphrase twice using hidden terminal input.
Choose a strong, unique passphrase and keep it separately from the node. There is
no plaintext passphrase flag. A terminal without hidden-input support is rejected;
the command never falls back to echoing input. The passphrase is not persisted in
Drive, bootstrap, receipts, node-secrets or the repository.

After Config/Soundbank/Memory final verification, provisioning uploads and verifies
the encrypted exact `LocalSecretStore` dataset, publishes/reuses a discoverable
descriptor, then publishes bootstrap. The provider remains Local. A failed backup
or descriptor CAS leaves bootstrap unchanged; successful earlier Config/Memory
transactions remain authoritative and are never rolled back. Immutable orphans
are retained. An identical rerun reuses the backup/descriptor and existing dataset
authorities, without unnecessary uploads or revisions.

Programmatic callers of `provision_cloud_state()` explicitly pass
`recovery_passphrase=` to request recoverable provisioning. Existing callers that
omit it retain the earlier Config/Soundbank/Memory-only behavior and do not gain
format-box secret recovery. The normal CLI always requests encrypted recovery.

For an already-provisioned Local or Cloud node, refresh the recovery backup after
Settings creates new secret references:

```bash
main/xiaozhi-server/.venv/bin/python scripts/provision_cloud_state.py --backup-secrets
```

This validates live Cloud state and updates only encrypted backups/descriptor
metadata, preserving bootstrap selection, Config/Memory revisions and runtime
files. Run it on each logical node that should be recoverable, with that node's
bootstrap and exact local secret store. Only nodes with published bundles are
offered for restore. A Memory reader can back up without taking writer authority.
When nodes select different Memory providers, a non-explicit node's backup retains
and validates the shared Memory authority still needed by other assigned nodes.

For deliberate passphrase rotation add `--rotate-recovery-passphrase`; the new
passphrase requires confirmation and produces a new immutable encrypted object.
Ordinary reruns must unlock the previous bundle with its existing passphrase and
reject a wrong passphrase. Old bundles remain history/orphans, so passphrase
rotation does not revoke access to previously downloaded encrypted backups.

## Source descriptor and discovery

One descriptor represents one folder/Config-manifest source, with a stable UUID,
display label, exact folder/Config/optional Memory manifest IDs and per-node
encrypted-secret pointers. It contains no secret plaintext, Memory/configuration
contents or authorization credentials. It is metadata, not a runtime authority.

Drive v2 private properties mark `xiaozhi_cloud_state=v1` and `source_id=<uuid>`
in the same initial upload as the descriptor. Discovery pages through marked files
and validates their property identity and descriptor. It uses the existing narrow
`drive.file` scope and never relies on filenames. Multiple sources are supported;
duplicate source identities/authorities and incomplete discovery fail safely.
See Google's [private file properties](https://developers.google.com/workspace/drive/api/guides/properties)
and [Drive v2 listing](https://developers.google.com/workspace/drive/api/reference/rest/v2/files/list).

Mutable descriptor updates use the same ETag read protection and conditional
publication transport as manifests, without changing Config/Memory protocols.
Adding a node preserves other node pointers. Publication follows encrypted blob
upload/download/hash/decryption verification. CAS conflict requires refresh and
review; it never silently overwrites another writer's metadata.

Initial source creation retains the existing V1 single-administrator assumption.
A source-bound creation receipt is durable before descriptor upload. A lost
upload response can be recovered by discovery on rerun. If discovery cannot find
an interrupted creation, provisioning stops for operator inspection rather than
creating a replacement descriptor. Concurrent initial creators are detected by
duplicate identity validation; do not initialize the same source concurrently.

## Encryption contract

Plaintext is deterministic canonical JSON with exactly `node_id` and `values`,
retaining every secret reference name and value, including references retained
for older active/LKG revisions. Cloud receives only authenticated ciphertext and
non-secret version/source/node/KDF/cipher metadata.

The existing `cryptography` dependency provides [scrypt](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#scrypt)
with a fresh 16-byte salt, `n=32768`, `r=8`, `p=1`, deriving a 256-bit key, followed
by [AES-GCM](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)
with a fresh 12-byte nonce and full authentication tag. Canonical AAD includes
`schema_version`, `source_id` and `node_id`. Wrong passphrase, substituted identity,
tampered ciphertext, bad pointer hash or unsupported crypto metadata produce a
constant error without exposing provider diagnostics. V1 bounds envelope size and
accepts only the fixed KDF parameters before deriving a key.

**Lose Google access + recovery passphrase → encrypted node secrets are
unrecoverable.** Google authorization alone does not decrypt the backup, and the
passphrase alone does not grant access to the app's Drive files. No unattended
decryption or locally stored recovery passphrase is implemented.

## Fresh Linux restore

Clone the repository and install its server dependencies, including the newly
pinned `google-auth-oauthlib`. This helper supplies Google's supported installed
application loopback OAuth flow with PKCE; it requests only `drive.file`.
See [Google's Desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app)
and [InstalledAppFlow](https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html).

**Application identity constraint:** this repository does not distribute a shared
Google OAuth client identity. Supply the Desktop OAuth client configuration of
the same application that owns the source. A new unrelated OAuth client cannot
discover those private properties or app-owned files with `drive.file`. Keep this
application configuration separate from user authorization credentials. It is not
an old node's token/refresh-token file. This additional application setup is an
explicit prerequisite, rather than a hidden claim of zero configuration.

From the repository root:

```bash
main/xiaozhi-server/.venv/bin/python scripts/restore_cloud_node.py \
  --oauth-client /secure/application-client.json --activate
```

For one-time OAuth client setup and headless authorization, see
[Google Drive OAuth setup](google-drive-oauth-setup.md). Restore automatically
uses `data/oauth-client.json` when neither explicit auth option nor the existing
default `data/drive-credentials.json` is available. SSH/headless sessions print
the login link and tunnel instructions without attempting to launch a browser.
The callback binds only to `127.0.0.1`, defaults to port `8765`, and waits up to
600 seconds. `--oauth-port`, `--oauth-timeout` (1–3600 seconds), and
`--ssh-target user@host` customize those instructions. A port collision rejects
login with an actionable message; it never silently picks another port.

Authorize Google, select the source/node if multiple exist, and enter the recovery
passphrase through hidden input. Single-source/single-node selection can be
automatic. With multiple nodes, enter an existing node ID from the displayed list.
If the normalized hostname exactly matches a listed node, the prompt shows
`Node ID [deskbox]:`; Enter accepts that default. Otherwise the prompt displays
the hostname for context and requires a valid existing ID. Unknown IDs reject
restore; it never creates a node. `node_id` remains the existing logical identity,
and a saved bootstrap identity is unchanged by hostname changes.
Explicit `--node-id` skips the node prompt:

```bash
main/xiaozhi-server/.venv/bin/python scripts/restore_cloud_node.py \
  --oauth-client /secure/application-client.json \
  --source-id SOURCE_UUID --node-id deskbox --activate
```

`--credentials /secure/authorized-user.json` preserves the existing authorized
credential workflow; this is optional and is not needed for format-box recovery.
If an existing default `data/drive-credentials.json` is present, it can be reused.
`--no-browser` prints the authorization URL instead of opening a browser. The
browser must reach the node's loopback callback, using an SSH tunnel when needed;
no deprecated out-of-band copy/paste authorization flow is used.

Without `--activate`, restored bootstrap selects Local and reports readiness to
switch explicitly. `--activate` explicitly selects `google_drive` for normal
startup. The restore command itself neither stages a runtime switch nor starts
listeners, materializes data or calls `mark_applied()`.

## Restore transaction and interruption

Credentials stay in memory during authorization/discovery. Restore verifies the
selected descriptor, encrypted pointer hash, AES-GCM authentication and exact
secret schema, then resolves live Config with a temporary in-memory secret
provider. It validates every required remote sound pointer/P3 and explicit Memory
manifest/snapshot, including Memory revision floors if a matching cache exists.
It rechecks the descriptor ETag after preflight. No Local Memory YAML, sound files,
desired/active caches or deployed identity is written during this validation.

After all checks pass, a private `data/.cloud-recovery/` staging journal records
fixed filenames and hashes. Individual files are atomically written and synced
in order: `data/drive-credentials.json` → hashed `data/node-secrets/*.json` →
`data/bootstrap.yaml` **last**. Files have mode 0600 and private directories mode
0700. The journal/staged plaintext has the same protection as the final local
credential/secret stores; no passphrase is staged. It is removed after durable
publication. Multi-file publication is journaled, not claimed to be one atomic
filesystem operation.

If publication stops partway, rerun for the same source/node and activation choice;
authorization and live verification repeat before resuming. A fresh node cannot
receive a usable Cloud bootstrap before its credentials/secrets are durable.
Corrupt stages, different pending identity, disagreeing existing bootstrap or
secret datasets reject restore. There is no force overwrite mode in V1.

Normal server startup then performs Config sync, Soundbank materialization and
Memory materialization. Only successful listener startup advances active LKG.
Recovery does not copy old `.config.yaml`, `config.d`, `.memory.yaml`, soundbank,
bootstrap, node-secrets or user credentials from the old box. OAuth app setup,
Google authorization and the human passphrase are the recovery prerequisites.

No remote GC, Memory merge/multi-writer, automatic failover, background backup,
generic data-directory backup or server/systemd installation is included.
