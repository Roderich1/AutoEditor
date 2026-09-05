from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from content_engine.domain.enums import RunStage, RunStatus

#: Bumped whenever the shape of manifest.json changes incompatibly.
MANIFEST_SCHEMA_VERSION = 1
#: Bumped whenever transcript.json or the normalization rules change incompatibly.
TRANSCRIPT_SCHEMA_VERSION = 1
#: Bumped whenever transcript/metrics.json changes incompatibly.
METRICS_SCHEMA_VERSION = 1


class MediaInfo(BaseModel):
    duration_seconds: float = Field(gt=0)
    video_codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    #: 0.0 means the container did not declare a usable frame rate.
    fps: float = Field(ge=0)
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    container: str
    file_size: int = Field(ge=0)


@dataclass(frozen=True)
class TranscriptionOptions:
    """What the user asked the transcriber to do."""

    model: str
    device: str
    compute_type: str
    beam_size: int
    word_timestamps: bool
    vad_filter: bool


@dataclass(frozen=True)
class ResolvedHardware:
    """What the transcriber will actually run on once ``auto`` is resolved."""

    device: str
    compute_type: str


@dataclass(frozen=True)
class RawWord:
    """Unvalidated provider output. May contain impossible timestamps."""

    word: str
    start: float
    end: float
    probability: float | None = None


@dataclass(frozen=True)
class RawSegment:
    start: float
    end: float
    text: str
    words: tuple[RawWord, ...] = ()


@dataclass(frozen=True)
class RawTranscription:
    language: str
    language_probability: float | None
    declared_duration_seconds: float | None
    segments: tuple[RawSegment, ...]
    model: str


class TranscriptWord(BaseModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptWord:
        if self.end < self.start:
            raise ValueError(f"word end ({self.end}) precedes start ({self.start})")
        return self


class TranscriptSegment(BaseModel):
    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[TranscriptWord]

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptSegment:
        if self.end < self.start:
            raise ValueError(f"segment end ({self.end}) precedes start ({self.start})")
        previous = self.start
        for word in self.words:
            if word.start < self.start or word.end > self.end:
                raise ValueError(
                    f"word '{word.word}' [{word.start}, {word.end}] falls outside "
                    f"segment {self.index} [{self.start}, {self.end}]"
                )
            if word.start < previous:
                raise ValueError(f"word '{word.word}' is out of order in segment {self.index}")
            previous = word.start
        return self


class Transcript(BaseModel):
    language: str
    language_probability: float | None
    #: Real audio duration, measured independently of the transcriber.
    duration_seconds: float = Field(gt=0)
    #: Duration the transcriber reported, kept so the two can be compared.
    declared_duration_seconds: float | None
    segments: list[TranscriptSegment]
    model: str
    created_at: datetime
    schema_version: int = TRANSCRIPT_SCHEMA_VERSION

    @model_validator(mode="after")
    def validate_segments(self) -> Transcript:
        previous_start = 0.0
        for position, segment in enumerate(self.segments):
            if segment.index != position:
                raise ValueError(
                    f"segment index {segment.index} is not contiguous at position {position}"
                )
            if segment.start < previous_start:
                raise ValueError(f"segment {segment.index} starts before its predecessor")
            if segment.end > self.duration_seconds:
                raise ValueError(
                    f"segment {segment.index} ends at {segment.end}, beyond the audio "
                    f"duration ({self.duration_seconds})"
                )
            previous_start = segment.start
        return self

    @property
    def word_count(self) -> int:
        return sum(len(segment.words) for segment in self.segments)


class NormalizationReport(BaseModel):
    """What had to be corrected in the provider output, and under which rules."""

    rules_version: int
    tolerance_seconds: float
    clamped_segment_bounds: int = 0
    clamped_word_bounds: int = 0
    dropped_empty_segments: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(
            self.clamped_segment_bounds or self.clamped_word_bounds or self.dropped_empty_segments
        )


class TranscriptionMetrics(BaseModel):
    schema_version: int = METRICS_SCHEMA_VERSION
    audio_duration_seconds: float
    declared_duration_seconds: float | None
    processing_seconds: float
    #: None when the audio duration is unusable rather than a misleading zero.
    real_time_factor: float | None
    segment_count: int
    word_count: int
    language: str
    language_probability: float | None
    model: str
    device_requested: str
    device_resolved: str
    compute_type_requested: str
    compute_type_resolved: str
    normalization: NormalizationReport


class InputManifest(BaseModel):
    path: Path
    sha256: str
    size: int = Field(ge=0)


class VersionManifest(BaseModel):
    content_engine: str
    python: str
    ffmpeg: str
    transcription_model: str
    analysis_provider: str
    analysis_model: str
    #: Populated by CE-026; null until the candidate prompt exists.
    prompt_version: str | None = None
    prompt_sha256: str | None = None


class StageRecord(BaseModel):
    """A completed stage and the inputs it was produced from."""

    fingerprint: str
    schema_version: int
    completed_at: datetime


class RunFailure(BaseModel):
    stage: RunStage
    error_type: str
    message: str
    occurred_at: datetime


class RunManifest(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    run_id: str
    created_at: datetime
    status: RunStatus
    input: InputManifest
    config_sha256: str
    versions: VersionManifest
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    failure: RunFailure | None = None
