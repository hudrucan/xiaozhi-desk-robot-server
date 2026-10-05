# Clone Node V1

The first-run wizard offers **Restore existing node** (the default) and **Create
a new node from selected backup**. Clone copies that backup's provider credentials
and secrets intentionally. It also copies the source node's environment, role and
node overrides; global/environment/role layers remain shared. With Config schema
V2, `cluster` is also shared: the clone inherits it automatically and copies only
the source assignment and explicit node exceptions, never a resolved runtime
snapshot. Migrate homogeneous V1 clones using the preview/apply procedure in
[Cloud Config](cloud-config-v1.md#explicit-shared-cluster-migration).
Soundbank pointers
continue to refer to immutable remote assets. No local cache, runtime directory
or materialized Memory file is copied.

Select the source backup node, enter a new ID (prefilled from the normalized
hostname), and provide the source recovery passphrase. New IDs allow letters,
digits, dots, underscores and hyphens, begin with a letter/digit, and are limited
to 192 characters. Both recovery descriptor and Cloud Config must lack this ID.
Legacy Config, including the old centralized frozen-defaults variant, is rejected.
Existing-node restore retains its original request and recovery flow.

Shared secret references still resolve from each node's private store. Clone
re-encrypts the selected backup's exact named values for the new identity; it does
not make shared Settings capable of distributing or rotating plaintext secrets.
An absent required reference fails preflight before activation.

## Publication and retry

The clone uses the existing descriptor, Config and encryption formats:

1. Rediscover and validate the selected descriptor and live centralized Config.
   Decrypt the source backup with **source** identity. Create a new dataset with
   the **new** node ID and copied secret values. Check local identity compatibility
   and the cloned node's effective Config, remote soundbank and Memory readiness.
2. Encrypt a fresh backup using the same passphrase, fresh salt/nonce and existing
   AAD bound to source UUID and **new** node ID. Persist ciphertext and a private
   intent under `data/.cloud-clone/` before publishing either mutable authority.
   Intent writes are atomic and fsynced; directory/files are 0700/0600. No recovery
   password or plaintext secrets are stored in this journal.
3. Upload and decrypt-verify the immutable new backup. Persist its unique pointer.
   Upload and verify the new Config object. Persist its exact proposed manifest
   before Config CAS, adding only the new node assignment. Read back and verify
   publication, then persist the Config receipt.
4. Read the latest descriptor and CAS in only the new node's backup pointer.
   Re-read both authorities and verify the expected assignment and unique pointer.
5. Delegate local recovery **as the new node** to the existing journal, which
   publishes credentials, new-node secrets and bootstrap last. Activate
   `google_drive`, then remove the first-run marker. No intermediate source-node
   bootstrap is ever deployed.

Config and descriptor are separate CAS authorities, so a partial remote addition
can temporarily be visible. There is no rollback/delete or remote GC. Retry the
same source UUID, source backup node, new node ID and passphrase, keeping the local
journal. The wizard restores and fixes these selections on reload. It prevents
switching to existing-node restore while a clone intent exists.

Retry accepts an already-published Config through its exact prepared manifest
(including a lost CAS response) or durable verified receipt plus the unchanged
cloned assignment. It accepts an existing descriptor entry only with this
transaction's unique backup pointer. Missing additions use fresh CAS against the
latest authority, preserving unrelated edits. Descriptor-only publication can
also resume. Successful local activation and marker-removal failures can retry
without another Cloud publication. Completed clone journals remain as private
receipts; the runtime does not consume them.

An equal assignment from another creator is insufficient ownership proof. A
competing ID, changed assignment, different encrypted pointer, corrupted journal,
changed source authorities or ambiguous publication is rejected safely. If the
Config CAS response was lost **and** another Config writer replaced that exact
manifest before a receipt could be saved, automatic ownership proof is unavailable;
the clone stops for review rather than adopting a potentially competing node.

The source node is untouched. Cloud Memory's manifest, snapshot and writer are
untouched; the clone uses the existing read-only recovery preflight and does not
claim write ownership. Normal Memory writer checks still apply after restart.

## Validation

Focused tests use fake Drive and fake OAuth, with disposable data and a localhost
HTTP test application. No actual robot server or external service is started:

```bash
cd main/xiaozhi-server
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_cloud_clone test_clone_setup test_cloud_recovery test_first_run_setup -q
```

For a manual smoke test, use a disposable fresh checkout and a recoverable test
source. Choose Clone, confirm the hostname default and credentials-copy warning,
reject an existing ID, then clone to a distinct ID. Verify the new assignment and
distinct encrypted backup in Cloud, bootstrap/secret dataset with the new ID,
unchanged source node and unchanged Memory writer. Restart and verify normal Cloud
startup. Test existing-node restore separately on another disposable checkout.
Interruption/CAS/local-publication recovery is covered with injected fake failures;
never delete the journal or restart normal runtime to bypass a partial clone.
