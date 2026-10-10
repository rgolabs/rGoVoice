"""ElevenLabs engine (paid): Instant Voice Cloning + text-to-speech.

API: https://elevenlabs.io/docs/api-reference
  POST /v1/voices/add                     multipart (name, description, files[]) -> {voice_id}
  POST /v1/text-to-speech/{voice_id}      json {text, model_id, voice_settings} -> audio bytes
  GET  /v1/models                         available models
  DELETE /v1/voices/{voice_id}
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .. import store
from .base import Engine, EngineError, EngineInfo, SynthResult

BASE_URL = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
FALLBACK_MODELS = [
    {"id": "eleven_multilingual_v2", "name": "Multilingual v2 (stable, 29 languages)"},
    {"id": "eleven_v3", "name": "Eleven v3 (most expressive)"},
    {"id": "eleven_flash_v2_5", "name": "Flash v2.5 (fast, cheap)"},
    {"id": "eleven_turbo_v2_5", "name": "Turbo v2.5"},
]
LANGUAGES = ["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh", "ja", "hu", "ko", "hi",
             "sv", "da", "fi", "no", "el", "he", "id", "ms", "ro", "sk", "uk", "vi", "bg", "hr", "ta", "fil"]


class ElevenLabsEngine(Engine):
    key = "elevenlabs"

    def __init__(self) -> None:
        self._models_cache: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------ helpers
    def _api_key(self) -> str:
        key = store.get_setting("elevenlabs_api_key") or ""
        if not key:
            raise EngineError("ElevenLabs API key is not set. Add it in Settings (or set ELEVENLABS_API_KEY).")
        return key

    def _client(self, timeout: float = 120.0) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, headers={"xi-api-key": self._api_key()}, timeout=timeout)

    @staticmethod
    def _raise(resp: httpx.Response, what: str) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json().get("detail")
            msg = detail.get("message") if isinstance(detail, dict) else detail
        except Exception:
            msg = resp.text[:300]
        raise EngineError(f"ElevenLabs {what} failed ({resp.status_code}): {msg}")

    def models(self, force: bool = False) -> list[dict]:
        if not force and self._models_cache and time.time() - self._models_cache[0] < 3600:
            return self._models_cache[1]
        try:
            with self._client(timeout=20) as c:
                r = c.get("/v1/models")
                self._raise(r, "model list")
                models = [
                    {"id": m["model_id"], "name": m.get("name") or m["model_id"]}
                    for m in r.json()
                    if m.get("can_do_text_to_speech", True)
                ]
        except EngineError:
            raise
        except Exception:
            models = FALLBACK_MODELS
        models = models or FALLBACK_MODELS
        # keep the stable default first
        models.sort(key=lambda m: (m["id"] != DEFAULT_MODEL, m["name"]))
        self._models_cache = (time.time(), models)
        return models

    def subscription(self) -> dict:
        with self._client(timeout=20) as c:
            r = c.get("/v1/user/subscription")
            self._raise(r, "subscription lookup")
            j = r.json()
            return {
                "tier": j.get("tier"),
                "character_count": j.get("character_count"),
                "character_limit": j.get("character_limit"),
                "voice_slots_used": j.get("voice_slots_used"),
                "voice_limit": j.get("voice_limit"),
                "can_use_instant_voice_cloning": j.get("can_use_instant_voice_cloning"),
            }

    # ------------------------------------------------------------ Engine API
    def info(self) -> EngineInfo:
        has_key = bool(store.get_setting("elevenlabs_api_key"))
        models = []
        if has_key:
            try:
                models = self.models()
            except EngineError:
                models = FALLBACK_MODELS
        return EngineInfo(
            key=self.key,
            name="ElevenLabs",
            kind="paid",
            description="Cloud Instant Voice Cloning. Best quality and speed; needs an ElevenLabs plan (Starter or above for cloning).",
            available=has_key,
            reason="" if has_key else "Add your ElevenLabs API key in Settings.",
            languages=LANGUAGES,
            models=models,
            options=[
                {"key": "model_id", "label": "Model", "type": "select", "default": DEFAULT_MODEL,
                 "choices": [{"value": m["id"], "label": m["name"]} for m in (models or FALLBACK_MODELS)]},
                {"key": "stability", "label": "Stability", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 0.5},
                {"key": "similarity_boost", "label": "Similarity", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 0.75},
                {"key": "style", "label": "Style exaggeration", "type": "range", "min": 0, "max": 1, "step": 0.05, "default": 0.0},
                {"key": "speed", "label": "Speed", "type": "range", "min": 0.7, "max": 1.2, "step": 0.05, "default": 1.0},
            ],
        )

    def train(self, voice: dict, sample_paths: list[Path], voice_dir: Path) -> dict:
        if not sample_paths:
            raise EngineError("Add at least one audio sample before training.")
        files = [("files", (p.name, p.read_bytes(), "audio/wav")) for p in sample_paths[:25]]
        data = {
            "name": f"{voice['name']} (rGoVoice)",
            "description": voice.get("description") or "Cloned with rGoVoice",
            "remove_background_noise": "true",
            "labels": '{"source":"rGoVoice"}',
        }
        with self._client(timeout=300) as c:
            # replace an earlier clone of this voice so we don't leak voice slots
            old = (voice.get("engines", {}).get(self.key, {}).get("data") or {}).get("voice_id")
            if old:
                c.delete(f"/v1/voices/{old}")
            r = c.post("/v1/voices/add", data=data, files=files)
            self._raise(r, "voice cloning")
            voice_id = r.json()["voice_id"]
        return {"voice_id": voice_id, "samples_used": len(files)}

    def synthesize(self, voice: dict, engine_data: dict, text: str, language: str, options: dict) -> SynthResult:
        voice_id = engine_data.get("voice_id")
        if not voice_id:
            raise EngineError("This voice has not been cloned on ElevenLabs yet.")
        model_id = options.get("model_id") or DEFAULT_MODEL
        settings = {
            "stability": float(options.get("stability", 0.5)),
            "similarity_boost": float(options.get("similarity_boost", 0.75)),
            "style": float(options.get("style", 0.0)),
            "use_speaker_boost": True,
            "speed": float(options.get("speed", 1.0)),
        }
        body: dict = {"text": text, "model_id": model_id, "voice_settings": settings}
        if language and model_id in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
            body["language_code"] = language
        with self._client(timeout=300) as c:
            r = c.post(f"/v1/text-to-speech/{voice_id}", params={"output_format": "mp3_44100_128"}, json=body)
            self._raise(r, "text-to-speech")
            return SynthResult(audio=r.content, mime="audio/mpeg", ext="mp3", sample_rate=44100,
                               meta={"model_id": model_id, "voice_settings": settings})

    def delete(self, voice: dict, engine_data: dict) -> None:
        vid = engine_data.get("voice_id")
        if not vid:
            return
        try:
            with self._client(timeout=30) as c:
                c.delete(f"/v1/voices/{vid}")
        except Exception:
            pass  # best effort; key may be missing/rotated
