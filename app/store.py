"""File-backed voice store.

Layout:
  data/voices/<voice_id>/voice.json       metadata (see new_voice)
  data/voices/<voice_id>/samples/*.wav    cleaned mono 16-bit reference samples
  data/voices/<voice_id>/engines/<key>/   engine artefacts (e.g. XTTS latents)
  data/outputs/<voice_id>/<output_id>.*   generated speech + .json sidecar
  data/settings.json                      API keys and defaults (0600)
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("RGOVOICE_DATA", Path(__file__).resolve().parent.parent / "data"))
VOICES_DIR = DATA_DIR / "voices"
OUTPUTS_DIR = DATA_DIR / "outputs"
SETTINGS_FILE = DATA_DIR / "settings.json"

_lock = threading.RLock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(path)


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                return json.loads(SETTINGS_FILE.read_text())
            except json.JSONDecodeError:
                return {}
        return {}


def save_settings(settings: dict) -> None:
    with _lock:
        _write_json(SETTINGS_FILE, settings, mode=0o600)


def get_setting(key: str, default: Any = None) -> Any:
    """Environment variable wins over settings file."""
    env = os.environ.get(key.upper())
    if env:
        return env
    return load_settings().get(key, default)


# ---------------------------------------------------------------- voices
def voice_dir(voice_id: str) -> Path:
    return VOICES_DIR / voice_id


def new_voice(name: str, language: str = "en", description: str = "") -> dict:
    vid = uuid.uuid4().hex[:12]
    voice = {
        "id": vid,
        "name": name.strip() or "Untitled voice",
        "description": description.strip(),
        "language": language or "en",
        "created_at": _now(),
        "updated_at": _now(),
        "samples": [],
        "engines": {},  # key -> {status, trained_at, error, data}
    }
    save_voice(voice)
    return voice


def save_voice(voice: dict) -> None:
    voice["updated_at"] = _now()
    with _lock:
        _write_json(voice_dir(voice["id"]) / "voice.json", voice)


def load_voice(voice_id: str) -> dict | None:
    p = voice_dir(voice_id) / "voice.json"
    if not p.exists():
        return None
    with _lock:
        return json.loads(p.read_text())


def list_voices() -> list[dict]:
    if not VOICES_DIR.exists():
        return []
    voices = []
    for d in VOICES_DIR.iterdir():
        v = load_voice(d.name) if d.is_dir() else None
        if v:
            voices.append(v)
    voices.sort(key=lambda v: v.get("created_at", ""), reverse=True)
    return voices


def delete_voice(voice_id: str) -> None:
    with _lock:
        shutil.rmtree(voice_dir(voice_id), ignore_errors=True)
        shutil.rmtree(OUTPUTS_DIR / voice_id, ignore_errors=True)


def sample_paths(voice: dict) -> list[Path]:
    base = voice_dir(voice["id"])
    return [base / s["file"] for s in voice.get("samples", []) if (base / s["file"]).exists()]


def next_sample_path(voice: dict, ext: str = "wav") -> tuple[str, Path]:
    idx = 1 + max([int(Path(s["file"]).stem.split("_")[0]) for s in voice.get("samples", [])] or [0])
    rel = f"samples/{idx:03d}_{uuid.uuid4().hex[:6]}.{ext}"
    return rel, voice_dir(voice["id"]) / rel


def engine_dir(voice: dict, engine_key: str) -> Path:
    d = voice_dir(voice["id"]) / "engines" / engine_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_engine_state(voice_id: str, engine_key: str, **fields) -> dict:
    """Atomically update one engine's state on disk and return the fresh voice."""
    with _lock:
        voice = load_voice(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        state = voice["engines"].setdefault(engine_key, {"status": "untrained", "trained_at": None, "error": None, "data": {}})
        state.update(fields)
        save_voice(voice)
        return voice


# ---------------------------------------------------------------- outputs
def output_dir(voice_id: str) -> Path:
    d = OUTPUTS_DIR / voice_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_output(voice_id: str, audio: bytes, ext: str, meta: dict) -> dict:
    oid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = output_dir(voice_id)
    (d / f"{oid}.{ext}").write_bytes(audio)
    record = {"id": oid, "voice_id": voice_id, "file": f"{oid}.{ext}", "created_at": _now(), **meta}
    _write_json(d / f"{oid}.json", record)
    return record


def list_outputs(voice_id: str) -> list[dict]:
    d = OUTPUTS_DIR / voice_id
    if not d.exists():
        return []
    items = []
    for p in d.glob("*.json"):
        try:
            items.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    items.sort(key=lambda r: r["id"], reverse=True)
    return items


def delete_output(voice_id: str, output_id: str) -> None:
    d = OUTPUTS_DIR / voice_id
    for p in d.glob(f"{output_id}.*"):
        p.unlink(missing_ok=True)
