from .base import Engine, EngineInfo, EngineError
from .xtts import XttsEngine
from .elevenlabs import ElevenLabsEngine

ENGINES: dict[str, Engine] = {
    XttsEngine.key: XttsEngine(),
    ElevenLabsEngine.key: ElevenLabsEngine(),
}

__all__ = ["ENGINES", "Engine", "EngineInfo", "EngineError"]
