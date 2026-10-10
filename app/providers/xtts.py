"""Local, free engine: Coqui XTTS-v2 (zero-shot voice cloning, 17 languages, runs on CPU / Apple MPS).

"Training" = computing the speaker embedding and GPT conditioning latents from the reference samples
and storing them next to the voice, so synthesis later never has to touch the samples again.

Note: the XTTS-v2 weights are released under the Coqui Public Model License (non-commercial use).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

from .. import store
from ..audio import read_wav, wav_bytes
from .base import Engine, EngineError, EngineInfo, SynthResult

log = logging.getLogger("rgovoice.xtts")

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
LANGUAGES = ["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"]
LATENTS_FILE = "latents.pt"
OUTPUT_SR = 24000


_SENT_END = re.compile(r"(?<=[.!?;:。！？；])\s+|(?<=[。！？])")
_SOFT_BREAK = re.compile(r"(?<=[,，、\-–—)])\s*|\s+")


def split_text(text: str, limit: int = 250) -> list[str]:
    """Split text into chunks of at most `limit` characters, preferring sentence boundaries,
    then commas, then whitespace. XTTS can only take ~400 tokens (≈250 chars) per inference call.
    Pure-regex replacement for XTTS's spaCy-based splitter; works for all 17 XTTS languages."""
    text = " ".join(text.split())
    if not text:
        return []
    limit = max(20, limit)

    def hard_split(s: str) -> list[str]:
        out: list[str] = []
        while len(s) > limit:
            cut = -1
            for m in _SOFT_BREAK.finditer(s):
                if 0 < m.end() <= limit:
                    cut = m.end()
                else:
                    break
            if cut <= 0:
                cut = limit
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
        return out

    sentences = [s.strip() for s in _SENT_END.split(text) if s and s.strip()]
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        for part in hard_split(sent):
            if cur and len(cur) + 1 + len(part) > limit:
                chunks.append(cur)
                cur = part
            else:
                cur = f"{cur} {part}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _pick_device() -> str:
    forced = os.environ.get("RGOVOICE_DEVICE")
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _preimport_patches() -> None:
    """coqui-tts refuses to import on torch>=2.9 unless torchcodec (which needs ffmpeg) is installed.
    The XTTS path never uses torchaudio/torchcodec file I/O once load_audio is patched below (all WAV
    reading goes through soundfile), so relax that import-time guard."""
    import transformers.utils.import_utils as iu

    iu.is_torchcodec_available = lambda *a, **k: True  # type: ignore[assignment]


def _patch_audio_loader() -> None:
    """XTTS loads reference audio with torchaudio.load, which needs ffmpeg/torchcodec on recent
    torchaudio builds. Our samples are always WAV, so read them with soundfile instead."""
    import torch
    from TTS.tts.models import xtts as xtts_mod

    def load_audio(audiopath, sampling_rate):
        import torchaudio

        audio_np, sr = read_wav(audiopath)
        audio = torch.from_numpy(audio_np).unsqueeze(0)
        if sr != sampling_rate:
            audio = torchaudio.functional.resample(audio, sr, sampling_rate)
        return audio.clip_(-1, 1)

    xtts_mod.load_audio = load_audio


class XttsEngine(Engine):
    key = "xtts"

    def __init__(self) -> None:
        self._model = None
        self._device = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._load_error: str | None = None
        self._importable = True
        try:
            import torch  # noqa: F401

            _preimport_patches()
            import TTS  # noqa: F401
        except Exception as e:  # pragma: no cover - env dependent
            self._importable = False
            self._load_error = f"coqui-tts / torch not importable: {e}"

    # ------------------------------------------------------------ model
    def _load(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            os.environ.setdefault("COQUI_TOS_AGREED", "1")
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts
            from TTS.utils.manage import ModelManager

            _patch_audio_loader()
            t0 = time.time()
            log.info("Downloading/locating XTTS-v2 weights (first run downloads ~1.9 GB)...")
            model_path, config_path, _ = ModelManager(progress_bar=True).download_model(MODEL_NAME)
            config = XttsConfig()
            config.load_json(str(config_path))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(config, checkpoint_dir=str(model_path), eval=True)
            device = _pick_device()
            try:
                model.to(device)
            except Exception as e:  # MPS fallbacks
                log.warning("Could not move XTTS to %s (%s); using cpu", device, e)
                device = "cpu"
                model.to(device)
            torch.set_grad_enabled(False)
            self._model, self._device = model, device
            log.info("XTTS-v2 ready on %s in %.1fs", device, time.time() - t0)
            return model

    @staticmethod
    def _models_dir() -> Path:
        return Path(os.environ.get("TTS_HOME", Path.home() / "Library/Application Support")) / "tts"

    def _weights_present(self) -> bool:
        d = self._models_dir() / MODEL_NAME.replace("/", "--")
        return (d / "model.pth").exists()

    # ------------------------------------------------------------ Engine API
    def info(self) -> EngineInfo:
        loaded = self._model is not None
        weights = self._weights_present()
        status = "loaded" if loaded else ("weights downloaded" if weights else "weights will download on first use (~1.9 GB)")
        return EngineInfo(
            key=self.key,
            name="XTTS-v2 (local, free)",
            kind="free",
            description=f"Runs on this machine, no API key. Device: {self._device or _pick_device()}. {status}.",
            available=self._importable,
            reason=self._load_error or "",
            languages=LANGUAGES,
            options=[
                {"key": "speed", "label": "Speed", "type": "range", "min": 0.6, "max": 1.6, "step": 0.05, "default": 1.0},
                {"key": "temperature", "label": "Expressiveness (temperature)", "type": "range", "min": 0.1, "max": 1.2, "step": 0.05, "default": 0.7},
                {"key": "repetition_penalty", "label": "Repetition penalty", "type": "range", "min": 1, "max": 15, "step": 0.5, "default": 8.0},
            ],
        )

    def train(self, voice: dict, sample_paths: list[Path], voice_dir: Path) -> dict:
        if not sample_paths:
            raise EngineError("Add at least one audio sample (6 seconds or more) before training.")
        import torch

        model = self._load()
        # gpt_cond_len=30 uses up to 30 s of reference audio (6 s chunks, averaged) instead of the 6 s default,
        # which noticeably improves timbre match; speaker embedding is averaged over all samples.
        kwargs = dict(audio_path=[str(p) for p in sample_paths], max_ref_length=30, gpt_cond_len=30,
                      gpt_cond_chunk_len=6, sound_norm_refs=True)
        with self._infer_lock:
            try:
                gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(**kwargs)
            except (RuntimeError, NotImplementedError) as e:
                if self._device == "cpu":
                    raise
                log.warning("XTTS conditioning failed on %s (%s); falling back to cpu", self._device, e)
                model.to("cpu")
                self._device = "cpu"
                gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(**kwargs)
        out = store.engine_dir(voice, self.key) / LATENTS_FILE
        torch.save(
            {"gpt_cond_latent": gpt_cond_latent.detach().cpu(), "speaker_embedding": speaker_embedding.detach().cpu()},
            out,
        )
        total = sum(read_wav(p)[0].shape[0] / read_wav(p)[1] for p in sample_paths)
        return {"latents_file": LATENTS_FILE, "samples_used": len(sample_paths), "reference_seconds": round(total, 1),
                "device": self._device}

    def _latents(self, voice: dict, engine_data: dict):
        import torch

        p = store.engine_dir(voice, self.key) / engine_data.get("latents_file", LATENTS_FILE)
        if not p.exists():
            raise EngineError("Stored voice latents are missing; train this voice again.")
        d = torch.load(p, map_location="cpu")
        return d["gpt_cond_latent"], d["speaker_embedding"]

    def synthesize(self, voice: dict, engine_data: dict, text: str, language: str, options: dict) -> SynthResult:
        model = self._load()
        lang = (language or voice.get("language") or "en").lower()
        if lang == "zh":
            lang = "zh-cn"
        if lang not in LANGUAGES:
            raise EngineError(f"XTTS does not support language '{language}'. Supported: {', '.join(LANGUAGES)}")
        gpt_cond_latent, speaker_embedding = self._latents(voice, engine_data)
        kwargs = dict(
            speed=float(options.get("speed", 1.0)),
            temperature=float(options.get("temperature", 0.7)),
            repetition_penalty=float(options.get("repetition_penalty", 8.0)),
            top_k=50,
            top_p=0.85,
            enable_text_splitting=False,  # we split ourselves (XTTS's splitter needs spaCy)
        )
        limit = int(model.tokenizer.char_limits.get(lang, 250))
        chunks = split_text(text, limit)
        if not chunks:
            raise EngineError("Nothing to say: the text is empty.")

        def run(chunk: str):
            return model.inference(chunk, lang, gpt_cond_latent, speaker_embedding, **kwargs)["wav"]

        t0 = time.time()
        pieces: list[np.ndarray] = []
        gap = np.zeros(int(OUTPUT_SR * 0.18), dtype=np.float32)  # short pause between sentences
        with self._infer_lock:
            for i, chunk in enumerate(chunks):
                try:
                    wav = run(chunk)
                except (RuntimeError, NotImplementedError) as e:
                    if self._device == "cpu":
                        raise
                    # An op unsupported on MPS/CUDA: fall back to CPU for this and later requests.
                    log.warning("XTTS inference failed on %s (%s); falling back to cpu", self._device, e)
                    model.to("cpu")
                    self._device = "cpu"
                    wav = run(chunk)
                wav = wav.detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav)
                if i:
                    pieces.append(gap)
                pieces.append(np.asarray(wav, dtype=np.float32).reshape(-1))
        wav = np.concatenate(pieces)
        elapsed = round(time.time() - t0, 2)
        return SynthResult(
            audio=wav_bytes(wav, OUTPUT_SR), mime="audio/wav", ext="wav", sample_rate=OUTPUT_SR,
            meta={"device": self._device, "seconds_to_generate": elapsed, "audio_seconds": round(len(wav) / OUTPUT_SR, 2)},
        )

    def delete(self, voice: dict, engine_data: dict) -> None:
        p = store.engine_dir(voice, self.key) / engine_data.get("latents_file", LATENTS_FILE)
        p.unlink(missing_ok=True)
