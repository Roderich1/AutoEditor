from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from content_engine.domain.enums import RunStage, RunStatus

#: Bumped whenever the shape of manifest.json changes incompatibly.
MANIFEST_SCHEMA_VERSION = 2
#: Bumped whenever transcript.json or the normalization rules change incompatibly.
TRANSCRIPT_SCHEMA_VERSION = 1
#: Bumped whenever transcript/metrics.json changes incompatibly.
METRICS_SCHEMA_VERSION = 1
#: Bumped whenever transcript/config.effective.json changes incompatibly.
TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION = 1


class _Model(BaseModel):
    """Base for every domain model that carries a real number.

    ``allow_inf_nan=False`` refuses NaN and the infinities at the boundary. They
    are not positions in audio, durations or probabilities, and JSON has no
    standard spelling for them: a transcript that absorbed one would either
    serialize to a document no conforming parser accepts, or propagate a value
    for which every comparison is false into arithmetic downstream. They are
    rejected, never coerced to zero and never clamped to a bound.
    """

    model_config = ConfigDict(allow_inf_nan=False)


class MediaInfo(_Model):
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

    provider: str
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


class TranscriptWord(_Model):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_interval(self) -> TranscriptWord:
        if self.end < self.start:
            raise ValueError(f"word end ({self.end}) precedes start ({self.start})")
        return self


class TranscriptSegment(_Model):
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


class Transcript(_Model):
    language: str
    language_probability: float | None = Field(ge=0, le=1)
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


class NormalizationReport(_Model):
    """What had to be corrected in the provider output, and under which rules."""

    rules_version: int
    tolerance_seconds: float = Field(gt=0)
    clamped_segment_bounds: int = 0
    clamped_word_bounds: int = 0
    dropped_empty_segments: int = 0
    notes: list[str] = Field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(
            self.clamped_segment_bounds or self.clamped_word_bounds or self.dropped_empty_segments
        )


class TranscriptionMetrics(_Model):
    schema_version: int = METRICS_SCHEMA_VERSION
    audio_duration_seconds: float = Field(gt=0)
    declared_duration_seconds: float | None
    processing_seconds: float = Field(ge=0)
    #: None when the audio duration is unusable rather than a misleading zero.
    real_time_factor: float | None = Field(ge=0)
    segment_count: int = Field(ge=0)
    word_count: int = Field(ge=0)
    language: str
    language_probability: float | None = Field(ge=0, le=1)
    model: str
    device_requested: str
    device_resolved: str
    compute_type_requested: str
    compute_type_resolved: str
    normalization: NormalizationReport


class InputManifest(_Model):
    path: Path
    sha256: str
    size: int = Field(ge=0)


class VersionManifest(_Model):
    content_engine: str
    python: str
    ffmpeg: str
    transcription_model: str
    analysis_provider: str
    analysis_model: str
    #: Which versioned prompt resource this run actually sent, and its digest.
    #: Null when none was sent -- a run analysed from a fixture, or one that has
    #: not been analysed at all.
    #:
    #: Narrower than the stage configuration's field of the same name, which
    #: records the prompt identity of whatever executed and for which a
    #: fixture's `fake-fixture/v1` is a truthful answer. Here a stand-in must
    #: leave null, or a reader asking "which prompt produced these candidates"
    #: gets the name of a prompt that was never sent anywhere.
    prompt_version: str | None = None
    prompt_sha256: str | None = None


class TranscriptionStageConfig(_Model):
    """The transcription settings that actually produced a transcript.

    The run-level ``config.effective.json`` records the configuration the
    experiment was *created* with. This records what the stage really ran, which
    differs whenever ``transcribe --config`` names another profile, and it
    resolves ``auto`` to the device and compute type the machine chose. It is the
    readable counterpart of the opaque fingerprint: the fingerprint decides
    whether a transcript may be reused, this explains why.
    """

    schema_version: int = TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION
    provider: str
    model: str
    beam_size: int = Field(gt=0)
    word_timestamps: bool
    vad_filter: bool
    device_requested: str
    device_resolved: str
    compute_type_requested: str
    compute_type_resolved: str
    normalization_version: int


class StageRecord(_Model):
    """A completed stage and the inputs it was produced from."""

    fingerprint: str
    #: Hash of the stage's effective configuration artifact, so the manifest and
    #: that artifact can be shown to belong together.
    stage_config_sha256: str
    schema_version: int
    completed_at: datetime


class RunFailure(_Model):
    stage: RunStage
    error_type: str
    message: str
    occurred_at: datetime


class RunManifest(_Model):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    run_id: str
    created_at: datetime
    status: RunStatus
    input: InputManifest
    config_sha256: str
    versions: VersionManifest
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    failure: RunFailure | None = None
