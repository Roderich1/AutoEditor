from pathlib import Path
from typing import Protocol

from content_engine.config import TranscriptionSettings
from content_engine.domain.models import Transcript


class TranscriberPort(Protocol):
    def transcribe(self, audio_path: Path, options: TranscriptionSettings) -> Transcript: ...
