# MQTT bootstrap for the Desk cluster

The opt-in standalone control plane serves firmware-compatible MQTT bootstrap at
`/xiaozhi/ota/`. HAProxy forwards that path through its existing floating management
VIP HTTP frontend to any of the three symmetric control planes. No leader or new
listener/process is added. The intended LAN URL is `http://<applied-vip>/xiaozhi/ota/`;
currently `http://192.168.1.186/xiaozhi/ota/`.

This deliberately extends the Settings-only composition with a small signer/time
handler. It does not import app.py, OTAHandler, WebSocketServer, ConnectionHandler,
logging bootstrap or provider initialization. app.py's legacy bootstrap behavior,
worker ping contract, firmware and physical hardware configuration are unchanged.
There is no firmware download, activation service, Vision endpoint, ASR/LLM/TTS,
MCP execution or conversation availability claim.

## Contract

The robot sends `Device-Id` (colon-separated MAC), `Client-Id` (persistent device
UUID), and JSON system metadata, using the existing firmware Ota request contract.
POST requires a JSON object <=16 KiB, read within two seconds. Duplicate fields,
malformed/nonfinite JSON, invalid identities and query parameters fail safely.
GET with valid identity headers also works. GET without identities returns only
credential-free readiness metadata for operators.

The response contains server time and timezone from the validated Cloud metadata,
an inert firmware object with an empty download URL, and MQTT configuration:

- endpoint: applied management VIP + configured MQTT port (1883);
- client ID: `GID_desk_robot@@@<MAC_with_underscores>@@@<persistent_client_id>`;
- username: base64 of a compact IP metadata object;
- password: existing gateway-compatible base64 HMAC-SHA256 over client ID + `|` + username;
- publish topic: `device-server`;
- subscribe topic: `devices/p2p/<MAC_with_underscores>`;
- keepalive: 240 seconds.

Credentials are per device/client identity. The gateway signing key itself is never
returned, logged or stored in Cloud Config. Response caching is disabled. The
bootstrap uses the existing gateway signing key from Vault; it never invents or
rotates a different key. Gateway selection remains balanced among eligible peers
and away from the Settings VIP; core selection remains separate.

The endpoint belongs to the existing trusted LAN. Identity headers are self-reported
as in the retained local bootstrap contract; the signature does not itself prove
physical device identity. An optional explicit MAC allowlist can restrict issuance.
It is not a public enrollment/authentication service. Do not expose it externally.

## Applied state and failure policy

Bootstrap requires operational HTTP, a usable validated local desired snapshot,
a root-managed applied ingress snapshot, and agreement between Cloud desired VIP
and the applied VIP. It reads cached metadata rather than live Drive per request.
If a future desired VIP differs from the currently applied VIP, credentials are
not issued for an unreachable new address: the endpoint returns unavailable until
a separately reviewed ingress rollout reconciles them. No VIP migration is done.

## Deployment

Environment controls:

- `XIAOZHI_MQTT_BOOTSTRAP_ENABLED`: true/false, default false;
- `XIAOZHI_BOOTSTRAP_MQTT_SIGNATURE_KEY`: existing gateway key, required when enabled;
- `XIAOZHI_BOOTSTRAP_MQTT_PORT`: default 1883;
- `XIAOZHI_BOOTSTRAP_INGRESS_STATE_FILE`: default `/etc/xiaozhi-ingress.json`;
- `XIAOZHI_BOOTSTRAP_ALLOWED_DEVICES`: optional JSON MAC list, default empty.

`mqtt-bootstrap.yml` performs whole-cluster source/authority/signing-key comparison,
then serially updates the existing control planes. Keep secret provisioning enabled
and supply its independent transfer Vault along with the existing transport Vault.
No extra dependency beyond current control-plane requirements is needed. No secret
value belongs in Git or public variable files.

## Acceptance

Use credentials fetched from this actual bootstrap to connect a simulated MQTT
client through the VIP, subscribe to the returned topic, send native hello, validate
encrypted UDP framing, ping, goodbye, reconnect and cleanup. Do not count manually
signed probe credentials as bootstrap acceptance. Test missing/invalid headers,
malformed bodies and desired/applied VIP drift separately with offline fixtures.

Finally the operator sets the robot's Server URL to the verified LAN bootstrap URL
and lets it restart/re-bootstrap. Firmware flashing/building is unnecessary. Actual
robot validation belongs to the user unless explicitly delegated. Confirm MQTT
connection and hello/UDP with gateway/core status and the existing role panel.
Conversation requests still report unavailable until their runtime is implemented.
