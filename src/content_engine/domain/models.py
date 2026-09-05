from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from content_engine.domain.enums import RunStatus


class MediaInfo(BaseModel):
    duration_seconds: float = Field(gt=0)
    video_codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    container: str
    file_size: int = Field(ge=0)


class TranscriptWord(BaseModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "TranscriptWord":
        if self.end < self.start:
            raise ValueError("word end must be greater than or equal to start")
        return self


class TranscriptSegment(BaseModel):
    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[TranscriptWord]

    @model_validator(mode="after")
    def validate_interval(self) -> "TranscriptSegment":
        if self.end < self.start:
            raise ValueError("segment end must be greater than or equal to start")
        return self


class Transcript(BaseModel):
    language: str
    language_probability: float | None
    duration_seconds: float = Field(ge=0)
    segments: list[TranscriptSegment]
    model: str
    created_at: datetime


class InputManifest(BaseModel):
    path: Path
    sha256: str
    size: int = Field(ge=0)


class ConfigManifest(BaseModel):
    sha256: str


class VersionManifest(BaseModel):
    content_engine: str
    ffmpeg: str
    python: str
    transcription_model: str
    analysis_provider: str
    analysis_model: str


class RunManifest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    run_id: str
    created_at: datetime
    input: InputManifest
    config: ConfigManifest
    versions: VersionManifest
    status: RunStatus
