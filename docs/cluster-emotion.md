# Distributed response emotion

The conversation core sends the existing firmware message
`{"type":"llm","text":"😔","emotion":"sad","session_id":"..."}` once per
LLM reply, before feeding its first nonempty text to TTS. It uses the same
supported emoji mapping and default `🙂` / `happy` as normal `app.py`. Empty or
whitespace chunks do not choose an emotion. Later text, tool continuation and
TTS segments do not replace that first choice. No additional emotion model or
provider is initialized. Existing TTS segmentation removes emoji from speech.

The authenticated firmware hello supplies `features.emoji`. Omission enables
the existing default; explicit `false` disables emotion messages and asks the
worker to avoid emoji. A boolean `emoji_enabled` is an optional field on the
tool-aware LLM request only. Workers append the existing response policy to
their local configured prompt when the field is present. Requests without the
field and the legacy text/final RPC contracts keep their previous behavior.
Full assistant text, Memory context and tool continuation remain unchanged.

Turn generation guards and abort cancellation prevent old response chunks from
updating a new turn's face. Fixed wake greetings and error speech do not create
a separate emotion source. Firmware keeps ownership of its face state and
existing listening/thinking/speaking transitions.

Deploy all workers accepting the optional request field before upgrading cores
that send it. No Cloud Config migration, runtime bundle re-export, new dependency
or firmware flash is required. An old worker rejects requests containing the
new field, so a mixed worker rollout must complete before core activation.

Offline checks, from `main/xiaozhi-server`:

```sh
python -m unittest discover -s tests -p 'test_cluster_emotion.py'
python -m unittest discover -s tests -p 'test_voice_transport.py'
python -m unittest discover -s tests -p 'test_cluster_mcp.py'
python -m unittest discover -s tests -p 'test_worker_llm_stream.py'
python -m unittest discover -s tests -p 'test_voice_error_recovery.py'
```

After rollout, ask for a cheerful and then a sad reply, check the face at reply
start, interrupt a reply and begin another, and repeat with a device tool call.
Verify normal speech and subtitles still work. Actual firmware behavior remains
a user-owned physical check.
