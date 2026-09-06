from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from content_engine.config import TranscriptionSettings
from content_engine.domain.exceptions import IncompatibleArtifactError
from content_engine.domain.models import (
    METRICS_SCHEMA_VERSION,
    TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION,
    ResolvedHardware,
    StageRecord,
    Transcript,
    TranscriptionMetrics,
    TranscriptionOptions,
    TranscriptionStageConfig,
)
from content_engine.domain.transcript_rules import (
    normalize_transcription,
    stage_config,
    stage_config_sha256,
    transcription_fingerprint,
)
from content_engine.ports.transcriber import TranscriberPort
from content_engine.utils.json import write_json, write_text
from content_engine.utils.timestamps import srt_timestamp

TRANSCRIPT_FILENAME = "transcript.json"
METRICS_FILENAME = "metrics.json"
#: The stage's own effective configuration, next to the artifacts it produced.
#: The run-level config.effective.json keeps the configuration the run was
#: created with; this one keeps what the stage actually ran.
STAGE_CONFIG_FILENAME = "config.effective.json"


def options_from_settings(settings: TranscriptionSettings) -> TranscriptionOptions:
    return TranscriptionOptions(
        provider=str(settings.provider),
        model=settings.model,
        device=str(settings.device),
        compute_type=str(settings.compute_type),
        beam_size=settings.beam_size,
        word_timestamps=settings.word_timestamps,
        vad_filter=settings.vad_filter,
    )


def read_stage_config(directory: Path) -> TranscriptionStageConfig:
    """Load the stage configuration an earlier run wrote, or refuse the artifact.

    Absence, unreadable bytes, invalid JSON, a shape this build does not
    understand and an unknown schema all mean the same thing to the caller: what
    is on disk cannot be shown to describe the transcript beside it. They are
    reported as one incompatibility rather than as four different failures.
    """
    path = directory.joinpath(STAGE_CONFIG_FILENAME)
    if not path.is_file():
        raise IncompatibleArtifactError(
            f"A transcript exists but {STAGE_CONFIG_FILENAME} is missing from {directory}, "
            "so there is no record of the settings that produced it. Rerun with --force."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IncompatibleArtifactError(
            f"{path} cannot be read as the configuration of the transcription stage: "
            f"{error}. Rerun with --force."
        ) from error
    if not isinstance(payload, dict):
        raise IncompatibleArtifactError(
            f"{path} does not contain a stage configuration object. Rerun with --force."
        )

    declared = payload.get("schema_version")
    if declared != TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares stage configuration schema {declared!r}; this build "
            f"understands {TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION}. The transcript was "
            "produced by a different version and is not reused. Rerun with --force."
        )
    try:
        return TranscriptionStageConfig.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid stage configuration: {error}. Rerun with --force."
        ) from error


def verify_stage_config(
    directory: Path, record: StageRecord, audio_sha256: str
) -> TranscriptionStageConfig:
    """Prove the stored explanation and the recorded digests still describe one run.

    The manifest carries two hashes and the run carries one readable artifact.
    Checking only the manifest would let an edited, deleted or mismatched
    artifact be reused behind a digest that still looks right, so both are
    recomputed from what is actually on disk. Nothing is written here: a refusal
    leaves the transcript, its configuration and the manifest untouched.
    """
    config = read_stage_config(directory)

    recomputed = stage_config_sha256(config)
    if recomputed != record.stage_config_sha256:
        raise IncompatibleArtifactError(
            f"{directory.joinpath(STAGE_CONFIG_FILENAME)} does not match the manifest "
            f"(recorded {record.stage_config_sha256[:12]}, recomputed {recomputed[:12]}). "
            "The stage configuration was changed after the transcript was produced. "
            "Rerun with --force."
        )

    rebuilt = transcription_fingerprint(audio_sha256, config)
    if rebuilt != record.fingerprint:
        raise IncompatibleArtifactError(
            f"The recorded fingerprint cannot be rebuilt from the audio and "
            f"{STAGE_CONFIG_FILENAME} (recorded {record.fingerprint[:12]}, rebuilt "
            f"{rebuilt[:12]}). The transcript, its configuration and the audio no longer "
            "describe one execution. Rerun with --force."
        )
    return config


@dataclass(frozen=True)
class TranscriptionOutcome:
    transcript: Transcript
    metrics: TranscriptionMetrics
    stage_config: TranscriptionStageConfig
    stage_config_sha256: str


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

        configuration = stage_config(options, hardware)

        # Nothing is written until the provider output has been validated, so a
        # stage that never completed leaves no effective configuration behind to
        # be mistaken for one that did.
        output_directory.mkdir(parents=True, exist_ok=True)
        write_json(
            output_directory.joinpath(TRANSCRIPT_FILENAME),
            transcript.model_dump(mode="json"),
        )
        write_json(output_directory.joinpath(METRICS_FILENAME), metrics.model_dump(mode="json"))
        write_json(
            output_directory.joinpath(STAGE_CONFIG_FILENAME),
            configuration.model_dump(mode="json"),
        )
        write_text(output_directory.joinpath("transcript.txt"), build_plain_text(transcript))
        write_text(output_directory.joinpath("transcript.srt"), build_srt(transcript))
        return TranscriptionOutcome(
            transcript=transcript,
            metrics=metrics,
            stage_config=configuration,
            stage_config_sha256=stage_config_sha256(configuration),
        )
