# Local speech assets

This directory contains the bundled defaults and supported local model
locations for the single-robot server:

| Path | Runtime |
| --- | --- |
| `vad/silero_vad.onnx` | Default Silero VAD |
| `asr/sherpa/vi-zipformer-int8/` | Vietnamese Sherpa transducer ASR |
| `tts/sherpa/vi-vivos-x-low/` | Vietnamese Sherpa VITS TTS |
| `tts/matcha-vi-int8/` | Local Vietnamese Matcha + Vocos TTS |

Cloud providers do not use these files. To enable the bundled Sherpa models,
install the optional local-speech dependencies:

```bash
pip install -r requirements-optional.txt
```

Then add local overrides in `data/.config.yaml`:

```yaml
selected_module:
  ASR: SherpaASR
  TTS: SherpaTTS

ASR:
  SherpaASR:
    model_dir: models/asr/sherpa/vi-zipformer-int8

TTS:
  SherpaTTS:
    language: vi
    model_dir: models/tts/sherpa/vi-vivos-x-low
    number_language: vi
```

Additional Sherpa model directories and the local Matcha model directory are
ignored by Git unless deliberately force-added as bundled defaults. Point the
matching provider configuration at the local directory.

Model licenses may differ from the server's MIT license. Keep the accompanying
model card and verify redistribution terms before replacing or adding assets.
