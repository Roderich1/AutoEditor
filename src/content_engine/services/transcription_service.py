from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from content_engine.config import TranscriptionSettings
from content_engine.domain.models import (
    METRICS_SCHEMA_VERSION,
    TRANSCRIPT_SCHEMA_VERSION,
    ResolvedHardware,
    Transcript,
    TranscriptionMetrics,
    TranscriptionOptions,
)
from content_engine.domain.transcript_rules import normalize_transcription
from content_engine.ports.transcriber import TranscriberPort
from content_engine.utils.json import write_json, write_text
from content_engine.utils.timestamps import srt_timestamp

TRANSCRIPT_FILENAME = "transcript.json"
METRICS_FILENAME = "metrics.json"


def options_from_settings(settings: TranscriptionSettings) -> TranscriptionOptions:
    return TranscriptionOptions(
        model=settings.model,
        device=str(settings.device),
        compute_type=str(settings.compute_type),
        beam_size=settings.beam_size,
        word_timestamps=settings.word_timestamps,
        vad_filter=settings.vad_filter,
    )


@dataclass(frozen=True)
class TranscriptionOutcome:
    transcript: Transcript
    metrics: TranscriptionMetrics


def build_srt(transcript: Transcript) -> str:
    """Numbered cues, contiguous from 1.

    Segments without text are already dropped during normalization; numbering is
    derived from the emitted cues so a gap can never appear in the sequence.
    """
    blocks = [
        "\n".join(
            (
                str(number),
                f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}",
                segment.text,
            )
        )
        for number, segment in enumerate(transcript.segments, start=1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def build_plain_text(transcript: Transcript) -> str:
    lines = [segment.text for segment in transcript.segments]
    return "\n".join(lines) + "\n" if lines else ""


class TranscriptionService:
    def __init__(
        self,
        transcriber: TranscriberPort,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transcriber = transcriber
        self.clock = clock

    def resolve_hardware(self, options: TranscriptionOptions) -> ResolvedHardware:
        return self.transcriber.resolve_hardware(options)

    def transcribe(
        self,
        audio_path: Path,
        audio_duration_seconds: float,
        output_directory: Path,
        options: TranscriptionOptions,
        hardware: ResolvedHardware,
    ) -> TranscriptionOutcome:
        started = self.clock()
        raw = self.transcriber.transcribe(audio_path, options, hardware)
        processing_seconds = self.clock() - started

        transcript, normalization = normalize_transcription(
            raw,
            audio_duration_seconds,
            datetime.now(UTC),
        )
        metrics = TranscriptionMetrics(
            schema_version=METRICS_SCHEMA_VERSION,
            audio_duration_seconds=audio_duration_seconds,
            declared_duration_seconds=transcript.declared_duration_seconds,
            processing_seconds=round(processing_seconds, 3),
            real_time_factor=(
                round(processing_seconds / audio_duration_seconds, 4)
                if audio_duration_seconds > 0
                else None
            ),
            segment_count=len(transcript.segments),
            word_count=transcript.word_count,
            language=transcript.language,
            language_probability=transcript.language_probability,
            model=transcript.model,
            device_requested=options.device,
            device_resolved=hardware.device,
            compute_type_requested=options.compute_type,
            compute_type_resolved=hardware.compute_type,
            normalization=normalization,
        )

        output_directory.mkdir(parents=True, exist_ok=True)
        write_json(
            output_directory.joinpath(TRANSCRIPT_FILENAME),
            transcript.model_dump(mode="json"),
        )
        write_json(output_directory.joinpath(METRICS_FILENAME), metrics.model_dump(mode="json"))
        write_text(output_directory.joinpath("transcript.txt"), build_plain_text(transcript))
        write_text(output_directory.joinpath("transcript.srt"), build_srt(transcript))
        return TranscriptionOutcome(transcript=transcript, metrics=metrics)

    @staticmethod
    def schema_version() -> int:
        return TRANSCRIPT_SCHEMA_VERSION
