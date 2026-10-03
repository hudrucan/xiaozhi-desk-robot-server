# Cloud Config V1

Cloud configuration is optional. Without `data/bootstrap.yaml`, the server uses
Local mode exactly as before: reference defaults plus private `.config.yaml` /
`config.d/` overrides, including recoverable atomic Settings transactions.
Firmware and audio/provider execution are unchanged.

## Local bootstrap

Create `main/xiaozhi-server/data/bootstrap.yaml` only when configuration source
selection is needed. This file is gitignored with the rest of `data/`:

```yaml
config_provider: local
node_id: mac-dev
google_drive:
  folder_id: PRIVATE_FOLDER_ID
  manifest_file_id: MANIFEST_FILE_ID
  credentials_path: data/drive-credentials.json
```

`node_id`, source selection and credential location belong exclusively to this
node. Drive objects cannot overwrite them. Credentials are never included in the
cloud object, LKG or public Settings responses. Relative credential paths are
resolved from `main/xiaozhi-server/`. Keep the credential file private (mode 0600).

## Provision a private Drive source

Enable Drive API for your Google project. Supply a locally pre-authorized Google
credential JSON file supported by `google.auth.load_credentials_from_file`, such
as authorized-user credentials with a refresh token. This implementation requests
only [`drive.file`](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
A folder manually created in Drive is not automatically accessible to an OAuth
app with this scope. The recommended provisioning path creates a private folder
through that app, so its folder, config objects and manifest are app-owned.
`--folder-id` remains available for a folder already granted to the same OAuth
app (for example through Google Picker). It does not implement an interactive
OAuth consent flow or broaden scopes to access arbitrary folders. A service
account needs suitable shared-drive access/storage quota; personal My Drive is
best provisioned with authorized-user credentials.

The transport uses `google-auth` and `requests`, both explicit pinned server
dependencies in `requirements.txt`. Credential loading is lazy, so offline
LKG startup does not need working OAuth credentials or an active Drive session.
The transport uses [Drive v2 file ETags](https://developers.google.com/workspace/drive/api/reference/rest/v2/files)
with `If-Match` for conditional publication. Uploaded objects use the documented
[small multipart upload flow](https://developers.google.com/workspace/drive/api/guides/manage-uploads).

From the repository root, deliberately copy current local layers into a **new**
Drive source:

```bash
main/xiaozhi-server/.venv/bin/python scripts/init_cloud_config.py \
  --create-folder "Xiaozhi Cloud Config" \
  --credentials-path data/drive-credentials.json \
  --node-id mac-dev \
  --from-local
```

This creates an app-owned private folder, one config object and one manifest.
It prints the folder and manifest IDs, verifies both uploads and does not switch
providers or overwrite any existing manifest. To reuse an app-accessible folder,
replace `--create-folder` with `--folder-id PRIVATE_FOLDER_ID`. The required
`--node-id` must match the local bootstrap identity receiving the imported
local overrides. `--from-local` imports normal overrides and replaces configured
secret fields with references. Actual values are written only to this node's
private `data/node-secrets/` store before any remote upload. Repository defaults
are not uploaded. Provisioning leaves the original Local files unchanged.
Never publish/share these files publicly. Put the resulting IDs and local
credential path into
`data/bootstrap.yaml`, keeping `config_provider: local` initially.

In Settings → **Configuration Source**, select Google Drive and click **Switch
source after restart**. The candidate's `validate_runtime_readiness()` requires
a live Drive refresh, the current node assignment, current repo defaults and
all cloud overlays, followed by node-local secret resolution and final effective
config validation. A missing required local secret rejects the switch with a
generic error containing neither values nor reference names. Bootstrap stays
Local, the running provider stays Local and pending provider remains unset.
Readiness never selects a runtime, marks a revision active, writes `active.json`,
uploads objects or updates the Drive manifest; it may refresh the local desired
cache. This method is reusable for node preflight checks. Only after it succeeds
is bootstrap selection saved and the running store's pending provider set.
Restart explicitly to use the new source.
Switching back to Local reads the retained local files; it never copies or merges
Drive configuration into them. Ordinary Save never changes the provider.
For offline setup, edit bootstrap to `google_drive` explicitly; initial boot then
requires a valid live Drive snapshot or a successfully applied `active.json`
for these exact folder/manifest IDs and local node identity. A desired-only cache
is insufficient: a new node must complete an online startup before it can boot
offline.

V1 source switching assumes one administrator and pre-provisioned sources.
It has no migration wizard or automatic local-to-cloud conflict resolution.

## Storage and transactions

### Schema decision for V1

V1 now encodes a centralized collection, so the upcoming dual-box phase can use
one manifest for shared configuration and cloud-managed assignments without
splitting a full config into separate per-node manifests. This task adds config
resolution only; it does not start workers or distribute ASR/TTS workloads.

An immutable canonical JSON object contains these scopes:

```json
{
  "schema_version": 1,
  "layers": {
    "global": {},
    "environments": {"dev": {}},
    "roles": {"server": {}},
    "nodes": {
      "mac-dev": {"environment": "dev", "role": "server", "overrides": {}}
    }
  }
}
```

The example is structural; each node's effective configuration must pass shared
Settings validation. Resolution order is current **repo defaults** (loaded with
`load_default_config()` from the running release) → cloud global overrides →
assigned environment → assigned role → node overrides. Upgrading server defaults
introduces new default values without changing the Drive object. Environment/role
assignments are stored in the cloud node record; nullable assignments inherit
repo defaults plus global overrides. `node_id` remains exclusively local bootstrap identity,
used to look up that record. An unassigned node fails safely rather than adopting
another node's config. Local bootstrap environment/role hints are not consulted.

Settings Save edits only the current node's overrides. It preserves shared
scopes, assignments and all other node records, without flattening inherited
values into the node layer. Every assigned node is validated before publishing
a shared config object using the current server release defaults. Topology editing
is supported through the CLI below; UI topology editing is out of scope.

Provisioning puts imported local overrides only in the specified node record.
Global/environment/role override maps start empty. Cloud layers cannot contain
bootstrap-only roots (`node_id`, `config_provider`, `google_drive`, or
`credentials_path`).

Legacy compatibility is explicit: iteration-1 `layers: {defaults, overrides}`
and iteration-2 centralized `global: {defaults, overrides}` retain their frozen
embedded-default semantics and their layout during ordinary Settings Save.
They are accepted only when every secret field is a reference or placeholder;
plaintext-bearing sources/caches are rejected, including offline startup.
Topology mutations require the new override-only layout. Reprovision explicitly
from retained Local configuration, review assignments, and select the new source;
there is no silent rewrite/migration. In the centralized format, a global object
with exactly the two keys `defaults` and `overrides` identifies iteration-2 legacy
layout; those keys are reserved for compatibility.

Canonical encoding is UTF-8 JSON, keys sorted, separators `,` and `:`, Unicode
preserved, and no NaN/Infinity.

The only mutable commit point is the manifest:

```json
{"schema_version":1,"revision":1,"config":{"file_id":"OBJECT_ID","sha256":"64-lowercase-hex-digits"}}
```

A cloud Save requires `base_revision` from the previous Settings read. The store
reads the latest manifest, compares revision, applies the existing Settings patch
and blank-secret rules, validates, uploads a new object, downloads it to verify
SHA-256, then updates the manifest **last** with its read ETag. Revision mismatch
or a concurrent conditional-update failure returns HTTP 409. Uploaded but
uncommitted objects can remain orphaned. No automatic merge, overwrite, Drive
revision-number reuse, rollout, watch API, or garbage collection is implemented.

If the manifest update's response is lost, the commit outcome may be unknown.
Sync and review current desired revision before retrying. A successful cloud
commit followed by disk-cache failure still reports Save success and exposes
`cache_error`, so a UI retry does not accidentally publish another revision.

Local mode revisions are content hashes of defaults and overrides. Drive mode
revisions are monotonically increasing application integers. Reuse of an existing
revision with different content and rollback to older revisions are rejected.

## Desired, active and LKG

- **Desired:** the authoritative Drive manifest, with a validated desired cache
  in `data/cloud-config/desired.json` (mode 0600). Save/manual Sync updates this
  cache, but it is **never an offline runtime fallback**.
- **Active / runtime LKG:** the exact snapshot and software-default fingerprint
  that completed successful startup, persisted only then to
  `data/cloud-config/active.json` (mode 0600).
- **Runtime:** the independent boot-selected snapshot, retained unchanged if a
  newer desired revision is saved or synced while startup is in progress.

Both caches include manifest/folder identity, revision and checksums. New caches
also bind local node identity. Centralized cache from another node is rejected;
legacy iteration-1 caches without node identity remain readable because their
configuration had no per-node selection.

The active envelope's checksum also covers `payload.repo_defaults_sha256`. This
is SHA-256 of the canonical JSON encoding of the parsed release defaults returned
by `load_default_config()`, including merged reference fragments. Mapping order,
YAML formatting and comments do not affect it. Runtime preparation captures one
parsed defaults object, uses that same object for layer resolution/validation and
fingerprinting, then resolves secrets. The fingerprint never hashes the resolved
runtime or stores default/secret values in the cache. Desired snapshots and Drive
objects/manifests carry no software-default fingerprint.

Offline runtime requires the current defaults fingerprint to match the active
fingerprint. Changed defaults invalidate that LKG for offline use: the node fails
safely until a live Drive boot with the new defaults completes successful startup.
Preparing or preflighting the online runtime does not rewrite active metadata;
`mark_applied()` records the fingerprint captured for the runtime that actually
started, even if the defaults file changes before Apply. A subsequent offline
boot can then use this newly applied snapshot with matching defaults.

An old active cache without a defaults fingerprint requires a successful online
boot to become an offline LKG again. This conservative check applies to all cloud
active caches, including legacy cloud layouts; their frozen layer resolution and
ordinary Save layout remain unchanged. No fingerprint is inferred or backfilled
from current defaults before successful startup.

Startup loads and validates desired and active caches separately, then attempts
Drive sync. A successful live sync selects that validated desired snapshot for
runtime. If Drive is unavailable, invalid, or cannot be read consistently,
**only a valid active snapshot with matching software defaults can run**.
A newer desired cache remains available
as desired metadata/editor state but does not get applied. With no valid active
snapshot, offline startup fails safely, even if desired.json is valid. Invalid
cloud content never overwrites active LKG; corrupt desired cache does not stop a
valid active snapshot from being used.

Save changes desired only; it never restarts the node. Manual Sync changes the
cached desired snapshot only. `active_revision` advances only after both HTTP
and WebSocket listeners confirm successful startup. If desired changes during
startup, Apply still records the boot-selected revision and cloud assignments.

Regression example: apply revision 1 → Save revision 2 without applying → Drive
offline → restart runs revision 1. Desired remains 2, active/runtime remain 1,
and sync state remains out_of_sync. No valid active.json means no offline boot.
Software-upgrade example: apply revision 1 using defaults A → install defaults B
→ offline restart fails without changing active.json → online restart uses B →
successful Apply records B's fingerprint → offline restart with B succeeds.

Settings includes `configuration_source` with current provider, node ID, desired
and active revision, sync state/status, conflict flag, last sync/error, source,
cache path and pending provider, plus desired/active environment and role,
runtime revision and runtime source. Manifest read races as well as final commit
CAS races preserve `ConfigConflict` and return HTTP 409 from Save/manual Sync.
Cloud desired/active mismatch reports
`out_of_sync` and `restart_required: true`. Local diagnostic-only edits retain
the existing no-restart behavior. Sync that discards unsaved browser edits
requires an explicit browser confirmation; failed Save/conflict retains edits.

Cache serializes only original reference-bearing layers, never the effective
configuration returned by secret resolution. Local provider behavior, including
plaintext Local overrides, sectioned atomic writes, backup/recovery, masking,
blank-secret retention and diagnostic-only no-restart saves is unchanged.
Soundbank config/authoring remains local to each node; this backend does not
upload audio assets or make node-specific filesystem paths portable.

## Node-local secrets

Cloud secret detection uses the same `is_secret_name()` helper as Settings,
including nested provider maps, lists and Authorization headers. Configured
secret plaintext is rejected in all cloud scopes. Empty/null values and the
existing `your_...` / `your-...` / Chinese template placeholders are allowed.
References must occupy the entire secret field; embedding them in public fields
or arbitrary text is rejected to prevent exposing resolved values through GET.
Do not place credentials in unrecognized public fields such as prompts/URLs.

```yaml
LLM:
  GeminiLLM:
    api_key: ${secret:GEMINI_API_KEY}
```

Seed this named reference on **each node that needs it**, using a hidden terminal
prompt in an interactive terminal (the command performs no Drive request):

```bash
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  set-local-secret GEMINI_API_KEY
```

`SecretProvider` defines local persistence and resolution independently of
ConfigEditor. Future encrypted/cloud providers can replace it through the store's
`secret_provider` boundary without changing Settings semantics. V1 uses
`data/node-secrets/<sha256-of-local-node-id>.json`, a gitignored atomic private
file (0600) in a private directory (0700). Resolution reads only the current
node's store after layer resolution and before provider initialization. Other
nodes' references can be seen in the shared object but their actual values are
absent from Drive, desired/active caches and this node's secret store. This is
configuration isolation between separately administered hosts; it does not
isolate processes sharing the same OS account/filesystem access.

Settings GET masks secret fields and never resolves them into its response.
Blank secret fields retain existing references/values. A new cloud Settings
secret is persisted locally under a new opaque immutable reference, then that
reference is committed through normal CAS. The active revision keeps its previous
secret until Apply succeeds. Named references are also immutable: rotate with a
new name, then change the cloud reference. Missing local references fail startup
safely with a generic error; values and reference names are not logged by this
backend. The local secret store must be backed up privately alongside bootstrap
and active LKG; caches alone are insufficient for recovery.

A failed upload/CAS can leave unused local secret entries and orphan reference-only
Drive objects. Entries are retained so active LKG references continue to resolve;
no automatic secret garbage collection or encryption backend is implemented.

## Topology administration

From the repository root (override files contain references, never plaintext
secrets), use the local bootstrap's Drive metadata and identity:

```bash
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py show
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  --base-revision 1 set-global --file global-overrides.yaml
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  set-environment production --file production-overrides.yaml
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  set-role worker --file worker-overrides.yaml
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  add-node deskbox-1 --environment production --role worker
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  add-node deskbox-2 --environment production --role worker
main/xiaozhi-server/.venv/bin/python scripts/cloud_config_admin.py \
  set-node deskbox-1 --environment production --role worker --file node-overrides.yaml
```

`--bootstrap PATH` selects another local bootstrap file. `--base-revision N`
optionally requires the revision previously inspected by `show`; put global
options before the operation. Otherwise the command captures the live revision
and ETag at its initial read. `set-* --file` **replaces** that scope's override
object; node assignments omitted from `set-node` remain unchanged. Add-node may
omit assignments and overrides. `set-node NAME --environment - --role -` clears
assignments. Deletion commands are `delete-environment NAME`, `delete-role NAME`
and `delete-node NAME`. Referenced environments/roles cannot be deleted. First
clear a node's assignments before deleting it; the administering local node
cannot be deleted and topology must retain at least one valid node.

Each mutation shares Settings' `commit_object_unlocked()` primitive: read/capture
revision and ETag → mutate a copy → validate every assigned node with current repo
defaults → upload immutable object → download/hash verify → CAS manifest last.
CLI override files are reference-only; use `set-local-secret` on the intended
node to provide values. No Drive JSON is edited in place, no silent merge occurs,
and provider selection/runtime/active LKG are unchanged. Successful mutations
print operation, old revision and new revision. Exit codes are 0 success, 2 invalid
input/state, 3 conflict and 4 cloud unavailable/unknown commit outcome. Conflicts
leave the winning manifest intact; read/review before retrying. `show` prints the
reference-only object and current revision without publication.

## Validation

No real credentials/network are required for unit tests:

```bash
cd main/xiaozhi-server
.venv/bin/python -m unittest discover -s tests -v
```

Before enabling Drive in a deployment, verify OAuth/folder access and conditional
manifest updates against that Drive account. Open two Settings clients, save in
one, and confirm the stale client receives a conflict. Verify cloud Save shows
different desired/active revisions until explicit Restart, then matching
revisions after both listeners start. Stop the server, make Drive unavailable,
and confirm restart uses active LKG. Specifically apply revision 1, Save
revision 2 without applying, then restart offline and confirm runtime stays at 1.
Check that desired-only cache without active.json cannot boot offline. Verify
Drive/cache objects contain references only, then replace a secret in
Settings and check offline LKG still resolves the old secret until Apply. On a
second node, seed only its own references and confirm it cannot resolve the first
node's references. Exercise CLI assignments and stale revision rejection. Confirm
Local Settings saves, secret masking and soundbank authoring still work.
No firmware change is required.

For a software-default upgrade, confirm offline startup rejects the old active
cache, then boot online and verify `active.json` receives the new fingerprint only
after both listeners start. Confirm a following offline startup succeeds. Before
switching Local to Drive, test with one required node-local secret absent and
verify switching fails without changing bootstrap/provider or writing active
LKG; provide the local reference and retry explicitly.
