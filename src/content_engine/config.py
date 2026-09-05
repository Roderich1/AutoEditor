from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from content_engine.domain.exceptions import ConfigurationError


class WorkspaceSettings(BaseModel):
    root: Path = Path("workspace")


class TranscriptionSettings(BaseModel):
    provider: str = "faster-whisper"
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = Field(default=5, gt=0)
    word_timestamps: bool = True
    vad_filter: bool = True


class ChunkingSettings(BaseModel):
    window_seconds: int = Field(default=360, gt=0)
    overlap_seconds: int = Field(default=30, ge=0)


class CandidateSettings(BaseModel):
    min_duration_seconds: float = Field(default=20, gt=0)
    max_duration_seconds: float = Field(default=90, gt=0)
    min_score: float = Field(default=65, ge=0, le=100)
    target_candidates: int = Field(default=10, gt=0)
    max_candidates: int = Field(default=15, gt=0)
    dedupe_iou: float = Field(default=0.60, ge=0, le=1)


class AnalysisSettings(BaseModel):
    provider: str = "openai"
    model: str = "SET_MODEL_HERE"
    prompt_version: str = "v1"
    chunking: ChunkingSettings = ChunkingSettings()
    candidates: CandidateSettings = CandidateSettings()


class PreviewSettings(BaseModel):
    enabled: bool = True
    width: int = Field(default=540, gt=0)
    height: int = Field(default=960, gt=0)


class RenderSettings(BaseModel):
    width: int = Field(default=1080, gt=0)
    height: int = Field(default=1920, gt=0)
    preset: str = "vertical_blur"
    video_codec: str = "libx264"
    encoder_preset: str = "medium"
    crf: int = Field(default=20, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    burn_subtitles: bool = True


class Settings(BaseModel):
    workspace: WorkspaceSettings = WorkspaceSettings()
    transcription: TranscriptionSettings = TranscriptionSettings()
    analysis: AnalysisSettings = AnalysisSettings()
    preview: PreviewSettings = PreviewSettings()
    render: RenderSettings = RenderSettings()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Cannot load configuration {path}: {error}") from error


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_settings(config_path: Path | None = None) -> Settings:
    default_path = project_root().joinpath("configs", "default.toml")
    values = _read_toml(default_path)
    if config_path is not None:
        resolved = config_path.expanduser().resolve()
        if resolved != default_path.resolve():
            values = _deep_merge(values, _read_toml(resolved))

    analysis_model = os.getenv("CONTENT_ENGINE_ANALYSIS_MODEL")
    if analysis_model:
        values.setdefault("analysis", {})["model"] = analysis_model

    try:
        settings = Settings.model_validate(values)
    except ValidationError as error:
        raise ConfigurationError(str(error)) from error

    root = settings.workspace.root.expanduser()
    if not root.is_absolute():
        root = project_root().joinpath(root)
    settings.workspace.root = root.resolve()
    return settings
