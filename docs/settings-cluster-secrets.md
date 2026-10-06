# API key provisioning through shared Settings

Standalone control-plane Settings now has a separate **Save key on all 3 nodes**
action next to a selected provider's API key. The normal Config Save still rejects
plaintext shared secrets. Normal app.py Settings behavior is unchanged; this
feature is mounted only by the standalone control-plane composition.

The feature is opt-in. Configure a separate 256-bit peer-transfer key in
`XIAOZHI_SECRET_PROVISION_KEY` (64 lowercase hex characters) and exactly three
node IDs/private HTTP endpoints in `XIAOZHI_SECRET_PROVISION_NODES` (JSON mapping).
Endpoints must be distinct private IPv4 addresses, with no credentials, paths,
queries or fragments, using the configured control-plane port. The local listener
must match its bootstrap identity. No node is a permanent coordinator; whichever
control plane serves the UI request orchestrates that operation.

The transfer key is infrastructure credential material, not a provider API key.
Ansible distributes it through root-only non-diffed environments. The
control-plane dependency set adds `cryptography==50.0.1`, already pinned in the
full server requirements. Providers/model runtimes are not installed or started.

## User flow

1. Open Settings through the management VIP and choose Providers.
2. Save or discard ordinary edits, including any provider selection change.
3. Enter the selected provider's API key and choose **Save key on all 3 nodes**.
4. The field is cleared after submission; the UI reports each node's readiness.

The key is not put into the ordinary Config patch, URLs, browser storage or public
responses. The UI cannot retrieve a stored value. Blank input does not remove an
existing key. Current scope supports selected-provider `api_key` fields for ASR,
LLM, VLLM, TTS, Memory and Intent; deletion and arbitrary secrets are not exposed.
This explicit all-node action rotates only the named credential on each deployed
member. Other node overrides and inactive Cloud assignments are preserved exactly;
the shared layers are unchanged.

Browser access retains the existing trusted-LAN HTTP Settings contract. It is
not HTTPS/public-network key management. Same-origin and an explicit custom JSON
header reject browser cross-site writes. Between nodes, the key and named
reference are AES-256-GCM encrypted/authenticated, even though private HTTP is used.
Both request and acknowledgement bind sender, recipient, direction, operation and
a short timestamp window. The recipient checks the real private peer address and
the encrypted Cloud authority binding; forwarded headers are never trusted.
HAProxy excludes `/internal/` from the management frontend.

## Publication and failure semantics

A rotation creates a fresh immutable reference, stages the key in each member's
existing private LocalSecretStore, and verifies durable read-back. The encrypted
acknowledgement proves the expected value, not merely HTTP success. Only after
all three acknowledge does the existing Cloud CAS-last publication save the new
reference in the three deployed node overrides. Shared layers and inactive
assignments remain unchanged. Cloud Config contains a reference, never the value. NATS carries only
the existing version/revision hint, never a key, reference name or ciphertext.

If a node is unavailable or an acknowledgement is invalid, no Config publication
is attempted. Existing references/values are preserved. Successfully staged but
unused references are retained; they are not deleted automatically. A concurrent
Cloud change causes a conflict and requires Sync/retry. Failure to publish the
NATS hint or refresh the public response after a successful CAS cannot turn that
commit into an apparent failed save. An uncertain Cloud/network outcome requires
Sync before retry, rather than claiming the save definitely failed.

Repeated authenticated peer delivery is idempotent for the same immutable
name/value within its timestamp window; attempts to change an existing name's
value fail. Public status contains node IDs/readiness/consistency only, with no
credential fingerprints, secret reference names, authority IDs or filesystem
paths. Different local values/reference exceptions are not claimed consistent.

## Runtime boundary

Provisioning advances **desired** config only. It does not initialize providers,
advance active/runtime revision, select a conversation startup, rewrite worker
bundles or restart processes. The later LLM deployment exports a node-bound
immutable bundle from the validated desired snapshot and local key store.
The original lightweight worker ping contract and empty capabilities are unchanged.

## API

- `GET /api/settings/secrets?group=LLM&provider=<selected-name>&field=api_key`
  returns per-node readiness, plus `ready` and `consistent` booleans.
- `POST /api/settings/secrets` accepts exactly `group`, `provider`, `field`,
  `value` and integer `base_revision`, with `X-Xiaozhi-Settings: 1`.
  It returns committed revision and safe node acknowledgements, plus masked
  Settings when available. Bodies are capped at 8 KiB and read within two seconds;
  values are bounded to 4 KiB and checked before any write.
- `POST /internal/settings/secret` accepts bounded authenticated ciphertext only.
  Peer HTTP requests have a four-second timeout, no redirects or environment
  proxies; the enclosing peer operation is bounded to five seconds. This endpoint
  is never a browser/management API. Source/target identities and Cloud authority
  must match explicitly provisioned deployment metadata.

Health policy remains validated Cloud snapshot + local assignment + operational
HTTP. Missing provider keys do not make a desired-only Settings node unhealthy.
Actual Gemini key validity/quota is checked by a subsequent provider call, not
claimed from storage acknowledgement.
