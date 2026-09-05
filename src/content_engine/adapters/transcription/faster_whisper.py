import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from content_engine.config import TranscriptionSettings
from content_engine.domain.exceptions import TranscriptionError
from content_engine.domain.models import Transcript, TranscriptSegment, TranscriptWord


def resolve_hardware(device: str, compute_type: str) -> tuple[str, str]:
    if device != "auto":
        return device, "int8" if compute_type == "auto" and device == "cpu" else compute_type
    try:
        ctranslate2 = importlib.import_module("ctranslate2")
        cuda_available = bool(ctranslate2.get_cuda_device_count() > 0)
    except (ImportError, RuntimeError):
        cuda_available = False
    return ("cuda", "float16") if cuda_available else ("cpu", "int8")


class FasterWhisperTranscriber:
    def transcribe(self, audio_path: Path, options: TranscriptionSettings) -> Transcript:
        try:
            faster_whisper = importlib.import_module("faster_whisper")
        except ImportError as error:
            raise TranscriptionError(
                "faster-whisper is not installed; run uv sync --extra transcription"
            ) from error

        device, compute_type = resolve_hardware(options.device, options.compute_type)
        try:
            model = faster_whisper.WhisperModel(
                options.model,
                device=device,
                compute_type=compute_type,
            )
            raw_segments, info = model.transcribe(
                str(audio_path.resolve()),
                beam_size=options.beam_size,
                word_timestamps=options.word_timestamps,
                vad_filter=options.vad_filter,
            )
            segments = [
                self._convert_segment(index, segment) for index, segment in enumerate(raw_segments)
            ]
        except Exception as error:
            raise TranscriptionError(f"Transcription failed: {error}") from error

        duration = max((segment.end for segment in segments), default=0.0)
        return Transcript(
            language=str(info.language),
            language_probability=getattr(info, "language_probability", None),
            duration_seconds=float(getattr(info, "duration", duration)),
            segments=segments,
            model=options.model,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _convert_segment(index: int, segment: Any) -> TranscriptSegment:
        text = " ".join(str(segment.text).split())
        words = [
            TranscriptWord(
                word=" ".join(str(word.word).split()),
                start=float(word.start),
                end=float(word.end),
                probability=getattr(word, "probability", None),
            )
            for word in (segment.words or [])
            if getattr(word, "start", None) is not None and getattr(word, "end", None) is not None
        ]
        return TranscriptSegment(
            index=index,
            start=float(segment.start),
            end=float(segment.end),
            text=text,
            words=words,
        )
