# Local speech models

Local ASR and TTS model files are runtime assets and are not committed to Git.

Install the optional Sherpa runtime from the server directory:

```bash
pip install -r requirements-optional.txt
```

## Vietnamese ASR

Download and prepare the 30M INT8 Zipformer model:

```bash
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2
tar xf sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2
mkdir -p models/asr/sherpa
mv sherpa-onnx-zipformer-vi-30M-int8-2026-02-09 models/asr/sherpa/vi-zipformer-int8
rm sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2
```

## Vietnamese TTS

Download and prepare the converted Piper/VITS medium voice:

```bash
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-vi_VN-vais1000-medium.tar.bz2
tar xf vits-piper-vi_VN-vais1000-medium.tar.bz2
mkdir -p models/tts/sherpa
mv vits-piper-vi_VN-vais1000-medium models/tts/sherpa/vi-medium
mv models/tts/sherpa/vi-medium/vi_VN-vais1000-medium.onnx models/tts/sherpa/vi-medium/model.onnx
rm models/tts/sherpa/vi-medium/vi_VN-vais1000-medium.onnx.json
rm models/tts/sherpa/vi-medium/MODEL_CARD
rm vits-piper-vi_VN-vais1000-medium.tar.bz2
```

Select `SherpaASR` and/or `SherpaTTS` in `data/.config.yaml` after their model
directories have been prepared.

The Sherpa runtime is Apache-2.0 licensed. Model licenses remain separate; the
VAIS1000 voice dataset is CC BY 4.0.
