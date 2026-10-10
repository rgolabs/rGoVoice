"""WAV helpers built on soundfile + numpy (no ffmpeg needed)."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import soundfile as sf


def read_wav(path_or_bytes) -> tuple[np.ndarray, int]:
    """Return mono float32 audio in [-1, 1] and its sample rate."""
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else str(path_or_bytes)
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return np.ascontiguousarray(mono, dtype=np.float32), int(sr)


def write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(audio, -1.0, 1.0), sr, subtype="PCM_16")


def wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.clip(audio, -1.0, 1.0), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def resample(audio: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    """Linear-interpolation resampler; good enough for reference audio and avoids extra deps."""
    if sr == target_sr or audio.size == 0:
        return audio
    n_out = int(round(audio.shape[0] * target_sr / sr))
    x_old = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def trim_silence(audio: np.ndarray, sr: int, threshold_db: float = -45.0, pad_ms: int = 150) -> np.ndarray:
    """Trim leading/trailing silence using a short-window RMS gate."""
    if audio.size == 0:
        return audio
    win = max(1, int(sr * 0.02))
    n = audio.shape[0] // win
    if n == 0:
        return audio
    frames = audio[: n * win].reshape(n, win)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    loud = np.where(db > threshold_db)[0]
    if loud.size == 0:
        return audio
    pad = int(sr * pad_ms / 1000)
    start = max(0, loud[0] * win - pad)
    end = min(audio.shape[0], (loud[-1] + 1) * win + pad)
    return audio[start:end]


def normalize_peak(audio: np.ndarray, peak_db: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 1e-6:
        return audio
    return (audio * (10 ** (peak_db / 20) / peak)).astype(np.float32)


def clean_sample(audio: np.ndarray, sr: int) -> np.ndarray:
    """Standard cleanup applied to every stored reference sample."""
    return normalize_peak(trim_silence(audio, sr))


def duration_s(audio: np.ndarray, sr: int) -> float:
    return round(audio.shape[0] / sr, 2) if sr else 0.0
