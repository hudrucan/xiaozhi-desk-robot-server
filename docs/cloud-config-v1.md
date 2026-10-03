# Cloud Config V1

Cloud configuration is optional. Without `data/bootstrap.yaml`, the server uses
Local mode exactly as before: reference defaults plus private `.config.yaml` /
`config.d/` overrides, including recoverable atomic Settings transactions.
Firmware and audio/provider execution are unchanged.

When bootstrap is absent or omits `node_id`, the default comes from the system
hostname: trim surrounding whitespace and one trailing dot, preserve case and
validate ASCII DNS labels (up to 192 characters overall). Invalid/empty/unavailable
hostnames use deterministic `local-node`. Reading bootstrap writes no files.
Once `node_id` is saved, hostname changes never rename that logical identity.

## Local bootstrap

Create `main/xiaozhi-server/data/bootstrap.yaml` only when configuration source
selection is needed. This file is gitignored with the rest of `data/`:

```yaml
config_provider: local
node_id: mac-dev
google_drive:
  folder_id: PRIVATE_FOLDER_ID
  manifest_file_id: MANIFEST_FILE_ID
  memory_manifest_file_id: MEMORY_MANIFEST_FILE_ID # Only for Cloud explicit memory
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

The initializer above creates a new Config source and imports configuration and
secrets only. For an existing folder/manifest, use the full-state provisioning
command below to reconcile Config, Soundbank and explicit Memory before switching.

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
It has no migration wizard or automatic conflict resolution between differing
Local and Cloud Memory datasets.

## Full-state provisioning into an existing source

For discoverable source metadata, encrypted node-secret backups and fresh Linux
recovery, see [Cloud State Recovery V1](cloud-state-recovery-v1.md). The provisioning
CLI now confirms a hidden recovery passphrase and publishes that metadata after
dataset verification; source switching remains a separate explicit action.

Keep `config_provider: local` and the existing `node_id`, Drive folder ID,
Config manifest ID and credential path in bootstrap. The folder must be accessible
to the same OAuth app with `drive.file`; provisioning checks its write capability
without broadening the scope or creating another Config source.

From the repository root:

```bash
main/xiaozhi-server/.venv/bin/python scripts/provision_cloud_state.py --from-local
```

Optional `--bootstrap` and `--local-config` select the bootstrap file and Local
override root. Source configuration uses the Local config store, including
sectioned `config.d`. This offline CLI imports the committed Local configuration.
When the provisioning function receives an existing Local store with a runtime
snapshot, it instead uses that snapshot's defaults and overrides, preserving
Save-without-Apply semantics. Neither path changes the running provider.

Provisioning has four ordered stages:

1. Validate Local configuration, secrets, every retained sound asset and its P3
   contract, and all explicit-memory device scopes with the lossless parser.
   Read and validate the existing Cloud topology, node assignment, revision,
   folder access and any existing Memory authority before uploading anything.
2. Reconcile only this node's overrides using the existing Settings candidate
   preparation and CAS-last publication. Keep global/environment/role semantics,
   other nodes and assignments. Persist node-local secrets before publication;
   Cloud configuration contains references only. Upload and verify canonical
   WAV/MP3/P3 and optimized P3 assets with pointers at their file-owning layers.
   Shared layers can gain dependent asset pointers without pinning inherited
   filenames into this node. Files owned only by repository defaults receive an
   explicit node file override so their pointers have a persisted owner.
3. For selected `mem_local_explicit`, seed a canonical immutable snapshot with
   every Local device scope, including inactive records and empty scopes. Verify
   download/hash/schema, then create and exactly verify a Memory manifest at
   revision 1 with bootstrap `node_id` as writer. Device IDs remain scope keys.
   Non-explicit Memory creates and requires no Memory authority.
4. Verify final live configuration, resolved secrets, remote asset bytes against
   Local files, and exact Memory contents/writer through a read-only verifier.
   Only then publish bootstrap using its durable writer, adding the Memory ID
   when applicable and keeping `config_provider: local`.

Local configuration, Memory YAML, sound files, deployment caches and active LKG
are not overwritten. Private node-local secrets and a reference-only provisioning
receipt are the only persistence needed before final bootstrap publication.
Final verification does not call runtime preparation, materialization, Memory
sync, source switching or `mark_applied()`.

An identical rerun refreshes the live state, reuses valid asset pointers and
matching secret references, and avoids a new Config revision or second Memory
authority. Existing Memory with matching exact contents and writer is reused at
its current revision, including revisions greater than 1. Differing contents,
another writer, invalid/missing referenced objects or incompatible inherited
configuration require explicit reconciliation; they are never replaced silently.
In particular, recursive layers cannot delete a shared key absent from Local, so
that case fails rather than changing the persisted layer format or shared scope.

Config and Memory are separate transactions. A successful Config CAS remains
authoritative if later Memory or bootstrap publication fails; Local remains
runnable and bootstrap does not declare completion. Failed asset publication or
Config CAS cannot publish Memory readiness. Immutable orphan objects are retained;
provisioning never rolls back a committed Config manifest or runs remote GC.

The source-bound receipt under `data/cloud-provisioning/` contains IDs and a
checksum, never secret/configuration/Memory contents. A returned Memory manifest
ID is saved before verification and bootstrap publication. Reruns strictly
validate that authority after transient verification or bootstrap-write failures.
A durable creation-intent receipt is written before manifest upload: if the
upload response is lost or the returned ID cannot be persisted, rerun stops for
explicit recovery instead of creating another authority. Recover the remote
manifest ID and the receipt/bootstrap metadata only after inspecting the outcome;
do not remove a pending receipt and blindly repeat creation.

The complete lifecycle is: run Local normally → explicitly provision all required
Cloud datasets → verify readiness → continue running Local → select Google Drive
in Settings and switch source after restart → restart → materialize verified
Soundbank/Memory caches during Cloud startup → call `mark_applied()` only after
both listeners start. Provisioning reports readiness but never stages the switch.

## Cloud State: explicit Memory V1

The three datasets have independent persistence lifecycles:

| Dataset | Authority and runtime behavior |
| --- | --- |
| Config | Revisioned desired/active snapshots; only applied `active.json` is offline runtime LKG. |
| Soundbank | Immutable binary assets with the content-addressed `data/cloud-soundbank/objects` cache. |
| Memory | Independent revisioned hot snapshots, device scopes, one authoritative writer and reader caches. |

In Local mode, `mem_local_explicit` keeps its configured YAML path and all existing
remember/forget/update/delete/recall behavior. It constructs no Cloud Memory store,
creates no Cloud Memory cache, and makes no Drive calls. Cloud Memory activates
only when `config_provider: google_drive` and the selected Memory provider's type
is `mem_local_explicit` (including provider aliases). Other Memory types need no
memory manifest metadata.

Cloud explicit memory requires the local-only
`google_drive.memory_manifest_file_id` bootstrap field. Missing/invalid metadata
rejects runtime preparation and source-switch readiness safely, with no Local
memory fallback. `provision_cloud_state.py --from-local` creates and verifies the
initial authority in the existing folder, then publishes its ID to Local bootstrap.
`init_cloud_config.py` does not seed Memory. At Cloud startup the manifest must
already exist and reference an immutable canonical JSON snapshot:

```json
{
  "schema_version": 1,
  "revision": 1,
  "writer_node_id": "deskbox",
  "snapshot": {"file_id": "IMMUTABLE_SNAPSHOT_ID", "sha256": "64_lowercase_hex_characters"}
}
```

The snapshot has exact shape `{"schema_version":1,"scopes":{"DEVICE_ID":[]}}`.
Each entry retains the explicit-memory fields `id`, `content`, `type`, `project`,
`entities`, `tags`, `importance`, `pinned`, `active`, `supersedes`, `created_at`,
and `updated_at`. Cloud validation requires already-normalized entries, preserves
inactive/superseded records and rejects malformed data. It verifies canonical
bytes, SHA, source identity, and revision rollback/reuse before accepting state.
The immutable objects contain memory content; keep the Drive source private.

Memory scopes remain **robot/device IDs** (`role_id=self.device_id`). Server
`node_id` is used only to authorize writes against the manifest's
`writer_node_id`. Other nodes can recall/list and sync, but cannot mutate. V1 has
no leases, promotion or multi-writer merge. Memory configuration (limits, aliases,
recall settings and budgets) stays in Cloud Config; memory records never advance
Config revision or require restart.

Cloud runtime preparation syncs Memory before constructing providers. It writes
`data/cloud-memory/current.json`, bound to the folder ID, memory manifest ID,
full manifest and snapshot, with an envelope checksum. It then atomically
materializes the configured YAML path (default `data/.memory.yaml`), preserving
the device-scope root format. Cache/YAML writes use temp, file fsync, replace and
directory fsync. Memory has one hot `current` revision, without desired/active
slots. Settings GET refreshes it; `sync_memory()` also provides explicit hot
refresh. Successful sync replaces runtime entries immediately. Recall/list use
the validated local state and make no network calls themselves.

If Drive is unavailable or a remote snapshot is invalid, a validated current
cache can restore YAML and serve reads. An actual manifest read/CAS conflict or
revision rollback/reuse is surfaced, never treated as a successful refresh.
Offline with missing/corrupt/source-mismatched current cache fails safely;
arbitrary old YAML is never Cloud authority. Reader and writer nodes both have
offline reads. All Cloud writes require live Drive; there is no outbox or
optimistic local mutation.

All four provider mutations share one transaction for both Settings and the
`manage_memory` tool: provider lock → Cloud Memory process/file lock → strict
live refresh → writer/base revision check → clone full snapshot → edit candidate
device scope → validate/canonicalize → immutable upload → download/SHA validation
→ manifest If-Match CAS **last**. Only successful CAS updates current cache,
YAML and live entries. Pre-CAS failures leave those unchanged. Failed CAS may
leave an immutable orphan; no remote GC is performed. If cache/YAML persistence
fails after CAS, remote success is reported, committed state remains in memory,
and `cache_error` plus a generic diagnostic requests sync recovery. Do not retry
the mutation as uncommitted. A process crash before local persistence needs
online recovery from the authoritative manifest.

Settings maps read-only/conflict to 409 and unavailable to 503. The tool catches
storage failures for both remember and forget. Errors omit Drive IDs, paths,
credentials and provider exception details. Additive inspection fields include
`storage_source`, `memory_revision`, `writable`, `writer_node_id`, `sync_state`
and `storage_error`.

Source switching supplies the **currently running source's** memory state to
candidate readiness. Local → Cloud uses the active Local effective config,
including provider aliases, configured path and entry limits. Saved-but-unapplied
settings do not replace that source config. The Cloud destination YAML is not
used as the source. A non-explicit Local source ignores unrelated stale YAML.
Missing/empty explicit Local memory is allowed; meaningful source records must
match the remote snapshot exactly after representation-neutral YAML parsing.
Source normalization is checked with Local settings, and any change to persisted
values (truncation, metadata repair, missing defaults or entry filtering) requires
explicit provisioning/reconciliation. Cloud limits cannot truncate Local records
into a false match.

Cloud → Local compares the committed hot snapshot with the Local candidate's
actual selected provider/path and settings. The materialized path may be reused;
a different path passes only if its records match without loss. Missing,
mismatched or lossy destination state rejects switching while meaningful Cloud
records exist. A post-CAS YAML write failure does not make stale YAML authoritative
for this comparison. Readiness never copies/migrates memory, creates a Memory
cache or writes either memory path. Bootstrap source selection is published only
after all readiness checks pass. Subsequent Local edits trigger the same guard
when switching back to Cloud.

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
semantic values into the node layer. Soundbank storage pointers are added to the
cloud layer owning the corresponding file definition, under the same CAS. Every assigned node
is validated before publishing a shared config object using the current server
release defaults. Topology editing
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

## Soundbank Cloud Assets

In Google Drive mode, Settings Save publishes every retained canonical WAV/MP3
and optimized P3 reference as an immutable binary object in the existing app-owned
folder. Canonical P3 entries are also supported. Each entry and its `optimized`
metadata can carry an optional `cloud: {file_id, sha256, size}` pointer. Names
contain the content hash; changed bytes under the same local filename receive a
new pointer. Matching current/candidate pointers and identical transaction assets
are reused. No listing, remote index or folder tree is required.

Pointers follow the effective `file` and `optimized.file` owners independently:
node overrides, global, assigned environment or assigned role. Only storage
metadata changes in a shared layer; an unrelated node Save does not copy inherited
file/text/provenance/audio-contract fields into node overrides. A legacy string
can become `{file, cloud}` in its original cloud layer. Later shared semantic
updates/deletions therefore continue to propagate to nodes without explicit
overrides. Repo defaults are software-owned: Save rejects an effective asset
whose file definition exists only there, until an explicit file override is
provided in a cloud-managed layer. Embedded defaults in legacy Drive layouts
remain cloud-owned. Candidate preparation runs once per Settings transaction;
direct store commits use the same preparation before publication.

Cloud layer resolution treats pointers as dependent metadata on every node.
An explicit canonical `file` override discards inherited canonical `cloud`;
an `optimized.file` override independently discards inherited `optimized.cloud`.
The new file owner can provide its own complete valid pointer. Partial pointers
cannot borrow fields from the previous file owner's pointer. Overrides of text,
provenance or audio metadata without a file override retain the inherited pointer;
string replacements retain scalar replacement semantics. This applies across
global/environment/role/node layers, legacy Cloud layouts, effective validation,
Settings reads and runtime/LKG resolution. Stored layers are not rewritten during
resolution. Generic Local configuration merging is unchanged.

Consequently, publishing a global pointer from one node cannot attach it to a
different node's overridden filename. That node remains pointerless for its own
file until it saves and publishes a pointer in the file's owning layer.

Save requires all referenced files locally, validates P3 structure and the existing
mono Opus/60ms audio contract, uploads missing objects, and downloads them to
verify size and SHA-256 before uploading the config object. Manifest CAS remains
last. Secret replacements are durable in the node-local store before any Drive
publication. A missing asset, upload or verification failure aborts publication
without advancing desired/active state or running post-save cleanup. A final CAS
conflict still returns HTTP 409; uploaded orphan objects can remain on Drive.

Every successfully published/materialized pointer-bearing asset also has a
verified immutable local copy in `data/cloud-soundbank/objects/<sha256>.<suffix>`
(beside a custom config cache when configured). Cache writes stage bytes, verify
SHA/size and P3 audio, then atomically publish. This directory must be outside the
runtime soundbank tree; Local mode never creates or uses it. V1 does not GC these
objects.

Before selecting startup runtime, pointer-bearing assets are materialized beneath
the configured local soundbank directory. Lookup order is matching runtime file,
verified content cache, then Drive download. Missing/corrupt files are restored
through staging, hash/size verified and P3 validated before atomic replacement. All required downloads are
validated before replacement begins. Traversal and symlink destinations are
rejected. Runtime continues to use `file` / `optimized.file`; TTS has no Drive
dependency and keeps optimized direct playback and canonical fallback.

Before attempting desired startup, all pointer-bearing assets needed by the
current active snapshot are retained in the content cache. Older active snapshots
without cached bytes are preserved from matching runtime files or downloaded
from Drive; if retention fails, desired materialization is aborted. Startup
cleanup can retire runtime filenames without touching the cache. If desired
startup overwrites a filename and later fails before listener confirmation,
`active.json` stays unchanged and an offline restart restores the exact old bytes
from the cache. `mark_applied()` still runs only after both listeners start.

Offline active LKG requires matching bytes in runtime files or content cache for
every pointer-bearing asset. Corrupt cached bytes are never trusted. If neither
local copy is valid and Drive is unavailable, startup fails safely and does not
mark the revision applied. Pointerless legacy string/object entries
retain the existing local-file/fallback behavior, including missing-file fallback.
Their next cloud Settings Save adds pointers if all referenced files exist.
Generate/optimize still creates local draft files; Save performs cloud publication.
Source-switch readiness validates configuration/secrets without writing assets;
startup performs materialization after an explicit restart.

Local mode retains the old schema, authoring, playback, atomic config writes and
reference-aware cleanup, adds no pointers and makes zero Drive calls. Cloud pointer
metadata does not change local saved/runtime/draft protections or cleanup journals.
There is no automatic remote deletion/GC: retired immutable objects remain usable
by another node's active/LKG revision.

Full-state provisioning publishes retained Local sound assets through this same
Settings preparation path. `init_cloud_config.py --from-local` imports config
metadata and secrets only. Topology administration does not publish local sound
binaries; use Cloud Settings Save or explicit full-state provisioning. Generic
data-directory synchronization and automatic background migration remain outside
the Cloud State workflow.

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
backend. Caches alone are insufficient for secret recovery. Publish the encrypted
node-secret backup described in [Cloud State Recovery V1](cloud-state-recovery-v1.md),
or retain a private local backup; refresh recovery metadata after adding references.

A failed upload/CAS can leave unused local secret entries and orphan reference-only
Drive objects. Entries are retained so active LKG references continue to resolve;
no automatic secret garbage collection is implemented. Runtime node-secrets remain
the existing local plaintext store; encryption applies to the optional remote
recovery bundle, not a redesigned runtime secret provider.

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

For Soundbank Cloud Assets, Save a retained canonical/optimized pair and inspect
both pointers. Stop the server, remove those local files, then restart online and
verify recovery before Apply. Restart offline with matching assets to check LKG;
repeat with one pointer-bearing file missing and confirm startup fails without
advancing active revision only when its content cache copy is also missing or
corrupt. Test desired startup with changed bytes under the same filename, fail
before Apply, then restart offline and verify the previous active bytes return.
Real-account binary upload/download and permissions
still require deployment verification.

For a software-default upgrade, confirm offline startup rejects the old active
cache, then boot online and verify `active.json` receives the new fingerprint only
after both listeners start. Confirm a following offline startup succeeds. Before
switching Local to Drive, test with one required node-local secret absent and
verify switching fails without changing bootstrap/provider or writing active
LKG; provide the local reference and retry explicitly.
