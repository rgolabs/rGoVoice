#!/usr/bin/env bash
# Start rGoVoice: http://localhost:8765
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No .venv found. Create it with:"
  echo "  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt"
  exit 1
fi

# Load optional .env (ELEVENLABS_API_KEY, RGOVOICE_DEVICE, TTS_HOME ...)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

export COQUI_TOS_AGREED="${COQUI_TOS_AGREED:-1}"      # XTTS-v2 weights are CPML licensed (non-commercial)
export TTS_HOME="${TTS_HOME:-$PWD/data/models}"       # where the ~1.9 GB XTTS weights are stored
export PORT="${PORT:-8765}"

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"
