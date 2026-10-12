# Cluster Soundbank authoring

The standalone control plane exposes Soundbank at `/xiaozhi/#soundbank` behind
the shared management VIP. Every configured control plane has the same editing
rights; the Cloud configuration manifest remains authoritative.

Generate requests a free worker through the existing targeted TTS segment
protocol. It requires workers at the saved revision with matching applied voice
identities. Existing admission excludes ASR-busy workers. The control plane
never imports a TTS provider or loads a model. Canonical mono WAV and optimized
Opus/P3 use the configured firmware audio rate.

Authoring returns HTTP 202 and an opaque job URL. Polling works through the
round-robin UI ingress: control planes locate the owning job through an
authenticated, encrypted private peer route. One operation per control plane is
allowed; at most 32 results are retained for one hour. Shutdown cancels owned
worker requests and waits for file/Cloud transactions. A coordinator restart
loses its unfinished job state, and the user must retry generation.

Generation uploads and verifies immutable Cloud blobs before returning entry
metadata, without changing the configuration revision. Signed draft preview
references use the existing shared peer key, are scoped to this Cloud authority
and filename/audio contract, and expire after 24 hours. Any node can preview
verified draft bytes, including after VIP failover. The preview route accepts
no credentials or arbitrary source URLs.

Save publishes the complete entry map with existing revision CAS. Renaming or
deleting entries affects shared desired state. All three nodes reconcile
verified Cloud caches in the background; the page reports readiness.
**Apply runtime** activates saved entries in conversations. Save and apply
provider/configuration changes before generating with a new worker revision.

Optimization also uses a job and verifies the saved/draft Cloud asset before
encoding. Authoring never modifies runtime directories. Control-plane Save
reuses verified content-addressed Cloud caches instead of requiring files in
its runtime directory. Local `app.py` authoring and cleanup remain unchanged.

Single-runtime filesystem cleanup is absent from the cluster UI. Entry deletion
does not delete immutable Cloud blobs or cache files that may be pinned by an
older active runtime. Cloud garbage collection needs a retention-aware procedure.

Requirements: configured three-node peer provisioning, existing segment TTS
workers, `ffmpeg`, and the already used `opuslib_next`/libopus codec. No model or
provider dependencies are installed in control planes.

Focused validation uses fake Cloud/workers and synthetic audio:

```bash
python -m unittest discover -s tests -p 'test_control_plane_soundbank.py'
python -m unittest discover -s tests -p 'test_control_plane.py'
python -m unittest discover -s tests -p 'test_runtime_apply_api.py'
```

Manual check: Generate and preview a short phrase; Save, wait for 3/3 verified
caches, then Apply runtime and speak the phrase on the firmware.
