from pathlib import Path
from typing import Protocol

from content_engine.domain.models import RawTranscription, ResolvedHardware, TranscriptionOptions


class TranscriberPort(Protocol):
    """Speech-to-text provider boundary.

    Hardware resolution is a separate call because the caller must know what the
    run will actually execute on before it decides whether an existing transcript
    can be reused.
    """

    def resolve_hardware(self, options: TranscriptionOptions) -> ResolvedHardware: ...

    def transcribe(
        self,
        audio_path: Path,
        options: TranscriptionOptions,
        hardware: ResolvedHardware,
    ) -> RawTranscription: ...
