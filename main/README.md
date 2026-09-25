# Xiaozhi core runtime

The product code lives in [`xiaozhi-server/`](xiaozhi-server/). This fork does
not include the upstream management, mobile, or digital-human applications.

For the project overview, installation, providers, Memory v2, endpoints, and
benchmarks, read the [repository README](../README.md).

## Start locally

```bash
cd xiaozhi-server
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
touch data/.config.yaml
python app.py
```

Local runtime files:

| Path | Purpose |
| --- | --- |
| `config.yaml` | Committed reference defaults |
| `data/.config.yaml` | Required, gitignored local overrides |
| `data/.memory.yaml` | Explicit Memory v2 records when enabled |
| `tmp/` | Logs and generated audio |

The server exposes the Xiaozhi WebSocket runtime on port `8000` and its
OTA/vision/Settings HTTP service on port `8003` by default.
