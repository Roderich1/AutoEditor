import importlib
from pathlib import Path
from typing import Any

from content_engine.domain.exceptions import ExternalProviderError, TranscriptionError
from content_engine.domain.models import (
    RawSegment,
    RawTranscription,
    RawWord,
    ResolvedHardware,
    TranscriptionOptions,
)


class FasterWhisperTranscriber:
    def resolve_hardware(self, options: TranscriptionOptions) -> ResolvedHardware:
        """Resolve ``auto`` conservatively.

        A GPU being present is not evidence that this CTranslate2 build can use
        it, so CUDA is only chosen when the runtime reports a usable device.
        """
        if options.device != "auto":
            compute_type = options.compute_type
            if compute_type == "auto":
                compute_type = "int8" if options.device == "cpu" else "float16"
            return ResolvedHardware(device=options.device, compute_type=compute_type)

        try:
            ctranslate2 = importlib.import_module("ctranslate2")
            cuda_available = bool(ctranslate2.get_cuda_device_count() > 0)
        except (ImportError, RuntimeError, OSError):
            cuda_available = False

        if cuda_available:
            compute_type = "float16" if options.compute_type == "auto" else options.compute_type
            return ResolvedHardware(device="cuda", compute_type=compute_type)
        compute_type = "int8" if options.compute_type == "auto" else options.compute_type
        return ResolvedHardware(device="cpu", compute_type=compute_type)

    def transcribe(
        self,
        audio_path: Path,
        options: TranscriptionOptions,
        hardware: ResolvedHardware,
    ) -> RawTranscription:
        try:
            faster_whisper = importlib.import_module("faster_whisper")
        except ImportError as error:
            raise TranscriptionError(
                "faster-whisper is not installed; run uv sync --extra transcription"
            ) from error

        try:
            model = faster_whisper.WhisperModel(
                options.model,
                device=hardware.device,
                compute_type=hardware.compute_type,
            )
            raw_segments, info = model.transcribe(
                str(audio_path.resolve()),
                beam_size=options.beam_size,
                word_timestamps=options.word_timestamps,
                vad_filter=options.vad_filter,
            )
            segments = tuple(self._convert_segment(segment) for segment in raw_segments)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExternalProviderError(
                f"faster-whisper failed on {hardware.device}/{hardware.compute_type}: {error}"
            ) from error

        declared = getattr(info, "duration", None)
        return RawTranscription(
            language=str(getattr(info, "language", "unknown")),
            language_probability=getattr(info, "language_probability", None),
            declared_duration_seconds=float(declared) if declared is not None else None,
            segments=segments,
            model=options.model,
        )

    @staticmethod
    def _convert_segment(segment: Any) -> RawSegment:
        words = tuple(
            RawWord(
                word=str(word.word),
                start=float(word.start),
                end=float(word.end),
                probability=getattr(word, "probability", None),
            )
            for word in (segment.words or [])
            if getattr(word, "start", None) is not None and getattr(word, "end", None) is not None
        )
        return RawSegment(
            start=float(segment.start),
            end=float(segment.end),
            text=str(segment.text),
            words=words,
        )
