from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class EngineError(Exception):
    """Raised by an engine for user-facing failures (bad key, missing samples, ...)."""


@dataclass
class EngineInfo:
    key: str
    name: str
    kind: str  # "free" | "paid"
    description: str
    available: bool
    reason: str = ""
    languages: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)  # UI option schema for synthesis
    models: list[dict[str, Any]] = field(default_factory=list)   # selectable backend models, if any


@dataclass
class SynthResult:
    audio: bytes
    mime: str
    ext: str
    sample_rate: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Engine:
    """A voice engine. `train` turns reference samples into a stored voice; `synthesize` speaks text with it."""

    key: str = "base"

    def info(self) -> EngineInfo:  # pragma: no cover - interface
        raise NotImplementedError

    def train(self, voice: dict, sample_paths: list[Path], voice_dir: Path) -> dict:
        """Return engine data to persist in voice["engines"][key]["data"]."""
        raise NotImplementedError

    def synthesize(self, voice: dict, engine_data: dict, text: str, language: str, options: dict) -> SynthResult:
        raise NotImplementedError

    def delete(self, voice: dict, engine_data: dict) -> None:
        """Release any remote/local resources tied to the trained voice."""
        return None
