"""rGoVoice HTTP API + static UI.

Run:  ./run.sh   (or)   .venv/bin/uvicorn app.main:app --port 8765
"""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import asdict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import store
from .audio import clean_sample, duration_s, read_wav, write_wav
from .providers import ENGINES, EngineError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rgovoice")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MIN_TOTAL_SECONDS = 6.0
MAX_TEXT_CHARS = 5000

app = FastAPI(title="rGoVoice", version="0.1.0")
_training: set[tuple[str, str]] = set()
_training_lock = threading.Lock()


def _voice_or_404(voice_id: str) -> dict:
    v = store.load_voice(voice_id)
    if v is None:
        raise HTTPException(404, "Voice not found")
    return v


def _engine_or_404(key: str):
    eng = ENGINES.get(key)
    if eng is None:
        raise HTTPException(404, f"Unknown engine '{key}'")
    return eng


def _public_voice(v: dict) -> dict:
    out = dict(v)
    out["total_sample_seconds"] = round(sum(s.get("duration", 0) for s in v.get("samples", [])), 1)
    out["engines"] = {k: {**st, "training": (v["id"], k) in _training} for k, st in v.get("engines", {}).items()}
    return out


# ------------------------------------------------------------------ pages
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ------------------------------------------------------------------ engines & settings
@app.get("/api/engines")
def engines():
    return [asdict(e.info()) for e in ENGINES.values()]


class SettingsIn(BaseModel):
    elevenlabs_api_key: str | None = None


@app.get("/api/settings")
def get_settings():
    s = store.load_settings()
    key = store.get_setting("elevenlabs_api_key") or ""
    return {
        "elevenlabs_api_key_set": bool(key),
        "elevenlabs_api_key_hint": (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else ""),
        "elevenlabs_from_env": bool(key) and not s.get("elevenlabs_api_key"),
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    s = store.load_settings()
    if body.elevenlabs_api_key is not None:
        if body.elevenlabs_api_key.strip():
            s["elevenlabs_api_key"] = body.elevenlabs_api_key.strip()
        else:
            s.pop("elevenlabs_api_key", None)
    store.save_settings(s)
    ENGINES["elevenlabs"]._models_cache = None  # type: ignore[attr-defined]
    return get_settings()


@app.get("/api/settings/elevenlabs/check")
def check_elevenlabs():
    try:
        eng = ENGINES["elevenlabs"]
        return {"ok": True, "subscription": eng.subscription(), "models": eng.models(force=True)}  # type: ignore[attr-defined]
    except EngineError as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ voices
class VoiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    language: str = "en"
    description: str = ""


@app.get("/api/voices")
def voices():
    return [_public_voice(v) for v in store.list_voices()]


@app.post("/api/voices", status_code=201)
def create_voice(body: VoiceIn):
    return _public_voice(store.new_voice(body.name, body.language, body.description))


@app.get("/api/voices/{voice_id}")
def get_voice(voice_id: str):
    return _public_voice(_voice_or_404(voice_id))


@app.patch("/api/voices/{voice_id}")
def update_voice(voice_id: str, body: VoiceIn):
    v = _voice_or_404(voice_id)
    v.update(name=body.name.strip(), language=body.language, description=body.description.strip())
    store.save_voice(v)
    return _public_voice(v)


@app.delete("/api/voices/{voice_id}", status_code=204)
def delete_voice(voice_id: str):
    v = _voice_or_404(voice_id)
    for key, st in v.get("engines", {}).items():
        try:
            ENGINES[key].delete(v, st.get("data") or {})
        except Exception as e:  # best effort
            log.warning("engine %s cleanup failed: %s", key, e)
    store.delete_voice(voice_id)


# ------------------------------------------------------------------ samples
@app.post("/api/voices/{voice_id}/samples", status_code=201)
async def add_sample(voice_id: str, file: UploadFile = File(...), label: str = Form("")):
    """Accepts WAV/FLAC/OGG/MP3 (anything libsndfile reads). The UI converts browser recordings to WAV first."""
    v = _voice_or_404(voice_id)
    raw = await file.read()
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(413, "Sample too large (max 60 MB)")
    try:
        audio, sr = read_wav(raw)
    except Exception as e:
        raise HTTPException(400, f"Could not decode audio ({e}). Upload WAV, FLAC, OGG or MP3.")
    audio = clean_sample(audio, sr)
    dur = duration_s(audio, sr)
    if dur < 1.0:
        raise HTTPException(400, "Sample is shorter than 1 second after trimming silence.")
    rel, path = store.next_sample_path(v)
    write_wav(path, audio, sr)
    v["samples"].append({"file": rel, "label": label or file.filename or "sample", "duration": dur, "sample_rate": sr})
    _invalidate_engines(v)
    store.save_voice(v)
    return _public_voice(v)


def _invalidate_engines(v: dict) -> None:
    """Samples changed: mark trained engines as stale (still usable, but a retrain is recommended)."""
    for st in v.get("engines", {}).values():
        if st.get("status") == "ready":
            st["stale"] = True


@app.get("/api/voices/{voice_id}/samples/{index}")
def get_sample(voice_id: str, index: int):
    v = _voice_or_404(voice_id)
    try:
        s = v["samples"][index]
    except IndexError:
        raise HTTPException(404, "Sample not found")
    return FileResponse(store.voice_dir(voice_id) / s["file"], media_type="audio/wav")


@app.delete("/api/voices/{voice_id}/samples/{index}")
def delete_sample(voice_id: str, index: int):
    v = _voice_or_404(voice_id)
    try:
        s = v["samples"].pop(index)
    except IndexError:
        raise HTTPException(404, "Sample not found")
    (store.voice_dir(voice_id) / s["file"]).unlink(missing_ok=True)
    _invalidate_engines(v)
    store.save_voice(v)
    return _public_voice(v)


# ------------------------------------------------------------------ training
def _train_job(voice_id: str, engine_key: str) -> None:
    try:
        v = store.load_voice(voice_id)
        if v is None:
            return
        eng = ENGINES[engine_key]
        data = eng.train(v, store.sample_paths(v), store.voice_dir(voice_id))
        store.set_engine_state(voice_id, engine_key, status="ready", trained_at=store._now(), error=None, data=data, stale=False)
        log.info("voice %s trained on %s: %s", voice_id, engine_key, data)
    except EngineError as e:
        store.set_engine_state(voice_id, engine_key, status="error", error=str(e))
    except Exception as e:
        log.error("training failed: %s", traceback.format_exc())
        store.set_engine_state(voice_id, engine_key, status="error", error=f"{type(e).__name__}: {e}")
    finally:
        with _training_lock:
            _training.discard((voice_id, engine_key))


@app.post("/api/voices/{voice_id}/train/{engine_key}", status_code=202)
def train_voice(voice_id: str, engine_key: str, background: BackgroundTasks):
    v = _voice_or_404(voice_id)
    eng = _engine_or_404(engine_key)
    info = eng.info()
    if not info.available:
        raise HTTPException(400, info.reason or f"{info.name} is not available")
    total = sum(s.get("duration", 0) for s in v["samples"])
    if total < MIN_TOTAL_SECONDS:
        raise HTTPException(400, f"Need at least {MIN_TOTAL_SECONDS:.0f} s of samples (have {total:.1f} s). 30 s to 2 min of clean speech works best.")
    with _training_lock:
        if (voice_id, engine_key) in _training:
            raise HTTPException(409, "Training already in progress")
        _training.add((voice_id, engine_key))
    store.set_engine_state(voice_id, engine_key, status="training", error=None)
    background.add_task(_train_job, voice_id, engine_key)
    return _public_voice(store.load_voice(voice_id))


@app.delete("/api/voices/{voice_id}/train/{engine_key}")
def untrain_voice(voice_id: str, engine_key: str):
    v = _voice_or_404(voice_id)
    eng = _engine_or_404(engine_key)
    st = v.get("engines", {}).pop(engine_key, None)
    if st:
        try:
            eng.delete(v, st.get("data") or {})
        except Exception as e:
            log.warning("engine cleanup failed: %s", e)
    store.save_voice(v)
    return _public_voice(v)


# ------------------------------------------------------------------ synthesis
class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    engine: str
    language: str | None = None
    options: dict = Field(default_factory=dict)


@app.post("/api/voices/{voice_id}/speak")
async def speak(voice_id: str, body: SpeakIn):
    v = _voice_or_404(voice_id)
    eng = _engine_or_404(body.engine)
    st = v.get("engines", {}).get(body.engine)
    if not st or st.get("status") != "ready":
        raise HTTPException(400, f"Voice is not trained on {eng.info().name} yet")
    lang = body.language or v.get("language") or "en"
    try:
        result = await run_in_threadpool(eng.synthesize, v, st.get("data") or {}, body.text, lang, body.options)
    except EngineError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("synthesis failed: %s", traceback.format_exc())
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    rec = store.save_output(voice_id, result.audio, result.ext, {
        "engine": body.engine, "language": lang, "text": body.text, "options": body.options,
        "mime": result.mime, "sample_rate": result.sample_rate, **result.meta,
    })
    rec["url"] = f"/api/voices/{voice_id}/outputs/{rec['id']}/audio"
    return rec


@app.get("/api/voices/{voice_id}/outputs")
def outputs(voice_id: str):
    _voice_or_404(voice_id)
    items = store.list_outputs(voice_id)
    for r in items:
        r["url"] = f"/api/voices/{voice_id}/outputs/{r['id']}/audio"
    return items


@app.get("/api/voices/{voice_id}/outputs/{output_id}/audio")
def output_audio(voice_id: str, output_id: str, download: bool = False):
    d = store.output_dir(voice_id)
    matches = [p for p in d.glob(f"{output_id}.*") if p.suffix != ".json"]
    if not matches:
        raise HTTPException(404, "Output not found")
    p = matches[0]
    mime = "audio/mpeg" if p.suffix == ".mp3" else "audio/wav"
    headers = {"Content-Disposition": f'attachment; filename="{p.name}"'} if download else {}
    return FileResponse(p, media_type=mime, headers=headers)


@app.delete("/api/voices/{voice_id}/outputs/{output_id}", status_code=204)
def delete_output(voice_id: str, output_id: str):
    _voice_or_404(voice_id)
    store.delete_output(voice_id, output_id)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
