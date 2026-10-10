# rGoVoice

Create and store cloned voices, then turn any text into speech in a chosen voice.
Two engines are built in, selectable per voice and per request:

| Engine | Cost | Where it runs | Quality / speed | Notes |
|---|---|---|---|---|
| **XTTS-v2** (Coqui) | free | this machine (CPU or Apple MPS) | good, several seconds per sentence on an M-series Mac | 17 languages, zero-shot cloning from 6 s+, weights ~1.9 GB, CPML licence (non-commercial) |
| **ElevenLabs** | paid (Starter plan+ for cloning) | ElevenLabs cloud | best, near real-time | Instant Voice Cloning, 30+ languages, needs an API key |

A voice is a folder of cleaned reference samples. "Training" a voice on an engine turns those samples
into something reusable: XTTS stores speaker latents locally, ElevenLabs stores a cloned voice id.
The same samples can be trained on both engines.

## Quick start

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./run.sh            # http://localhost:8765
```

Then in the browser:

1. **+ New** creates a voice profile (name, language).
2. **Record** with the mic (there is a sample script to read) or **Upload** wav/mp3/m4a/flac/ogg files.
   Aim for 30 s to 2 min of clean, single-speaker speech. Files are converted to 16-bit WAV in the
   browser and silence-trimmed/normalised on the server.
3. **Train** on XTTS (free) and/or ElevenLabs (paid). XTTS downloads its weights on the first run.
4. Type text, pick the engine, language and options, **Generate speech**. Every result is kept in the
   voice's history with a download link.

### ElevenLabs key

Settings (⚙) → paste the key → Save → "Check connection" shows your plan, quota and whether instant
cloning is enabled. The key is stored in `data/settings.json` (mode 0600). Alternatively set
`ELEVENLABS_API_KEY` in `.env`.

## Configuration (`.env`, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `ELEVENLABS_API_KEY` | – | overrides the key saved in the UI |
| `RGOVOICE_DEVICE` | auto (`mps` on Apple Silicon, else `cpu`) | device for XTTS |
| `TTS_HOME` | `./data/models` | where XTTS weights are stored (point at a bigger disk if needed) |
| `RGOVOICE_DATA` | `./data` | voices, outputs, settings |
| `PORT` | `8765` | HTTP port |

## Layout

```
app/
  main.py              FastAPI routes (voices, samples, train, speak, outputs, settings)
  store.py             file-backed storage under data/
  audio.py             WAV read/write, trim, normalise (soundfile + numpy, no ffmpeg)
  providers/base.py    Engine interface
  providers/xtts.py    free local engine (Coqui XTTS-v2)
  providers/elevenlabs.py  paid cloud engine
static/                single-page UI (vanilla JS)
data/                  created at runtime, git-ignored
  voices/<id>/voice.json, samples/*.wav, engines/xtts/latents.pt
  outputs/<voice id>/<output id>.wav|mp3 + .json
  settings.json
```

## API

| Method & path | Purpose |
|---|---|
| `GET /api/engines` | engines, availability, languages, option schema |
| `GET/PUT /api/settings` | ElevenLabs key |
| `GET /api/settings/elevenlabs/check` | plan, quota, model list |
| `GET/POST /api/voices`, `GET/PATCH/DELETE /api/voices/{id}` | voice profiles |
| `POST /api/voices/{id}/samples` (multipart `file`, `label`) | add a sample |
| `GET/DELETE /api/voices/{id}/samples/{index}` | play / remove a sample |
| `POST /api/voices/{id}/train/{engine}` | train in the background; poll `GET /api/voices/{id}` |
| `DELETE /api/voices/{id}/train/{engine}` | drop trained data (deletes the ElevenLabs clone too) |
| `POST /api/voices/{id}/speak` `{text, engine, language, options}` | synthesize; returns output record with `url` |
| `GET /api/voices/{id}/outputs`, `.../outputs/{oid}/audio?download=1`, `DELETE .../outputs/{oid}` | history |

Interactive docs: http://localhost:8765/docs

## Notes

- First XTTS use downloads ~1.9 GB to `TTS_HOME` and takes about a minute to load; later loads are a few seconds.
  Set `RGOVOICE_DEVICE=cpu` if MPS misbehaves.
- XTTS splits long text into sentences automatically; ElevenLabs accepts up to 5000 characters per request.
- Adding or removing samples marks trained engines as "stale"; they keep working but a retrain is suggested.
- If the first XTTS download was interrupted, delete `data/models/tts/tts_models--multilingual--multi-dataset--xtts_v2` and train again; the model manager does not resume partial downloads.
- Only clone voices you have the right to use.
