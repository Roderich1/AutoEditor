from __future__ import annotations

import json
import os
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from content_engine.domain.enums import (
    AnalysisProvider,
    ComputeType,
    Device,
    RenderPreset,
    TranscriptionProvider,
)
from content_engine.domain.exceptions import ConfigurationError
from content_engine.utils.hashing import sha256_bytes

RESOURCE_PACKAGE = "content_engine.resources"
DEFAULT_CONFIG_RESOURCE = "default.toml"
WORKSPACE_ENV_VAR = "CONTENT_ENGINE_WORKSPACE"
ANALYSIS_MODEL_ENV_VAR = "CONTENT_ENGINE_ANALYSIS_MODEL"

#: Fields describing where this machine stores things rather than what the run
#: computes. They are written to config.effective.json but excluded from
#: config_sha256 so the same logical experiment hashes identically anywhere.
#: Any future workspace field must be evaluated individually before being added:
#: a field that changes processing or artifacts belongs in the hash.
ENVIRONMENT_ONLY_FIELDS: tuple[tuple[str, str], ...] = (("workspace", "root"),)


class _Section(BaseModel):
    """Base for configuration sections.

    Sections declare types and invariants but never default values: the packaged
    ``resources/default.toml`` is the single source of defaults.
    """

    model_config = ConfigDict(extra="forbid")


def _require_even_dimensions(section: str, width: int, height: int) -> None:
    for name, value in (("width", width), ("height", height)):
        if value % 2 != 0:
            raise ValueError(f"{section}.{name} ({value}) must be even for H.264 encoding")


class WorkspaceSettings(_Section):
    root: Path


class TranscriptionSettings(_Section):
    provider: TranscriptionProvider
    model: str = Field(min_length=1)
    device: Device
    compute_type: ComputeType
    beam_size: int = Field(gt=0)
    word_timestamps: bool
    vad_filter: bool


class ChunkingSettings(_Section):
    window_seconds: int = Field(gt=0)
    overlap_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> ChunkingSettings:
        if self.overlap_seconds >= self.window_seconds:
            raise ValueError(
                f"analysis.chunking.overlap_seconds ({self.overlap_seconds}) must be lower "
                f"than analysis.chunking.window_seconds ({self.window_seconds})"
            )
        return self


class CandidateSettings(_Section):
    min_duration_seconds: float = Field(gt=0)
    max_duration_seconds: float = Field(gt=0)
    min_score: float = Field(ge=0, le=100)
    target_candidates: int = Field(gt=0)
    max_candidates: int = Field(gt=0)
    dedupe_iou: float = Field(ge=0, le=1)
    boundary_snap_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> CandidateSettings:
        if self.min_duration_seconds >= self.max_duration_seconds:
            raise ValueError(
                f"analysis.candidates.min_duration_seconds ({self.min_duration_seconds}) must be "
                f"lower than analysis.candidates.max_duration_seconds "
                f"({self.max_duration_seconds})"
            )
        if self.target_candidates > self.max_candidates:
            raise ValueError(
                f"analysis.candidates.target_candidates ({self.target_candidates}) must not "
                f"exceed analysis.candidates.max_candidates ({self.max_candidates})"
            )
        return self


class AnalysisSettings(_Section):
    provider: AnalysisProvider
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    chunking: ChunkingSettings
    candidates: CandidateSettings


class PreviewSettings(_Section):
    enabled: bool
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_dimensions(self) -> PreviewSettings:
        _require_even_dimensions("preview", self.width, self.height)
        return self


class RenderSettings(_Section):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    preset: RenderPreset
    video_codec: str = Field(min_length=1)
    encoder_preset: str = Field(min_length=1)
    crf: int = Field(ge=0, le=51)
    audio_codec: str = Field(min_length=1)
    audio_bitrate: str = Field(min_length=1)
    burn_subtitles: bool

    @model_validator(mode="after")
    def validate_dimensions(self) -> RenderSettings:
        _require_even_dimensions("render", self.width, self.height)
        return self


class Settings(_Section):
    workspace: WorkspaceSettings
    transcription: TranscriptionSettings
    analysis: AnalysisSettings
    preview: PreviewSettings
    render: RenderSettings


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


def read_packaged_defaults() -> dict[str, Any]:
    """Read the canonical defaults shipped inside the installed package."""
    resource = resources.files(RESOURCE_PACKAGE).joinpath(DEFAULT_CONFIG_RESOURCE)
    try:
        with resource.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            f"Cannot load packaged defaults {RESOURCE_PACKAGE}/{DEFAULT_CONFIG_RESOURCE}: {error}"
        ) from error


def config_sources(config_path: Path | None = None) -> list[str]:
    """Describe every configuration layer, in merge order."""
    sources = [f"packaged {RESOURCE_PACKAGE}/{DEFAULT_CONFIG_RESOURCE}"]
    if config_path is not None:
        sources.append(str(config_path.expanduser().resolve()))
    if os.getenv(ANALYSIS_MODEL_ENV_VAR):
        sources.append(f"env {ANALYSIS_MODEL_ENV_VAR}")
    if os.getenv(WORKSPACE_ENV_VAR):
        sources.append(f"env {WORKSPACE_ENV_VAR}")
    return sources


def resolve_workspace_root(root: Path) -> Path:
    """Resolve a workspace root against the current working directory.

    The workspace is a property of the execution environment, never of the
    installation directory, so a relative value follows the invoking shell.
    """
    expanded = root.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd().joinpath(expanded)
    return expanded.resolve()


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<root>"
        lines.append(f"  - {location}: {issue['msg']}")
    return "Invalid configuration:\n" + "\n".join(lines)


def load_settings(config_path: Path | None = None) -> Settings:
    values = read_packaged_defaults()
    if config_path is not None:
        values = _deep_merge(values, _read_toml(config_path.expanduser().resolve()))

    analysis_model = os.getenv(ANALYSIS_MODEL_ENV_VAR)
    if analysis_model:
        values = _deep_merge(values, {"analysis": {"model": analysis_model}})
    workspace_root = os.getenv(WORKSPACE_ENV_VAR)
    if workspace_root:
        values = _deep_merge(values, {"workspace": {"root": workspace_root}})

    try:
        settings = Settings.model_validate(values)
    except ValidationError as error:
        raise ConfigurationError(_format_validation_error(error)) from error

    settings.workspace.root = resolve_workspace_root(settings.workspace.root)
    return settings


def canonical_config(settings: Settings) -> dict[str, Any]:
    """The subset of the configuration that identifies the experiment.

    ``config.effective.json`` stores the complete configuration for diagnostics;
    ``config_sha256`` covers only this logical subset, so the same experiment
    hashes identically across machines, path separators and operating systems.
    """
    data = settings.model_dump(mode="json")
    for section, field in ENVIRONMENT_ONLY_FIELDS:
        section_data = data.get(section)
        if isinstance(section_data, dict):
            section_data.pop(field, None)
    return data


def config_sha256(settings: Settings) -> str:
    payload = json.dumps(
        canonical_config(settings),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256_bytes(payload.encode("utf-8"))
