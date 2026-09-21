# Local speech models

Local ASR and TTS model files are runtime assets and are not committed to Git.

Install the optional Sherpa runtime from the server directory:

```bash
pip install -r requirements-optional.txt
```

Download compatible models from the upstream sources:

- [Sherpa ONNX source](https://github.com/k2-fsa/sherpa-onnx)

Keep ASR and TTS assets under their respective provider directories, then set
the model paths and filenames in `data/.config.yaml`. The committed
`config.yaml` intentionally contains placeholders and does not select a
specific model.

Sherpa ONNX is Apache-2.0 licensed. Each downloaded model may use a different
license; retain and review its accompanying model card.
