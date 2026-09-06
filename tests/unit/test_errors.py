"""Error classification, exit codes and the CLI behaviour they drive."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.config import WORKSPACE_ENV_VAR
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_ANALYSIS,
    EXIT_CONFIGURATION,
    EXIT_INVALID_INPUT,
    EXIT_RENDER,
    EXIT_TRANSCRIPTION,
    EXIT_UNKNOWN,
    AnalysisError,
    AudioExtractionError,
    ConfigurationError,
    ContentEngineError,
    CorruptArtifactError,
    ExternalProviderError,
    ExternalToolNotFoundError,
    IncompatibleArtifactError,
    InvalidCandidateError,
    InvalidMediaError,
    InvalidRunIdError,
    InvalidRunStateError,
    NoAudioStreamError,
    RenderError,
    RunNotFoundError,
    TranscriptionError,
    TranscriptionProviderError,
    UnsupportedSchemaVersionError,
)
from content_engine.domain.models import MANIFEST_SCHEMA_VERSION
from tests.conftest import fake_process

runner = CliRunner()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConfigurationError("x"), EXIT_CONFIGURATION),
        (ExternalToolNotFoundError("x"), EXIT_CONFIGURATION),
        (InvalidMediaError("x"), EXIT_INVALID_INPUT),
        (NoAudioStreamError("x"), EXIT_INVALID_INPUT),
        (AudioExtractionError("x"), EXIT_INVALID_INPUT),
        (RunNotFoundError("x"), EXIT_INVALID_INPUT),
        (InvalidRunIdError("x"), EXIT_INVALID_INPUT),
        (InvalidRunStateError("x"), EXIT_INVALID_INPUT),
        (CorruptArtifactError("x"), EXIT_INVALID_INPUT),
        (UnsupportedSchemaVersionError("x"), EXIT_INVALID_INPUT),
        (IncompatibleArtifactError("x"), EXIT_INVALID_INPUT),
        (TranscriptionError("x"), EXIT_TRANSCRIPTION),
        (TranscriptionProviderError("x"), EXIT_TRANSCRIPTION),
        (AnalysisError("x"), EXIT_ANALYSIS),
        (ExternalProviderError("x"), EXIT_ANALYSIS),
        (InvalidCandidateError("x"), EXIT_ANALYSIS),
        (RenderError("x"), EXIT_RENDER),
        (ContentEngineError("x"), EXIT_UNKNOWN),
    ],
)
def test_every_exception_maps_to_its_exit_code(error: ContentEngineError, expected: int) -> None:
    assert error.exit_code == expected


def test_missing_external_tool_is_a_configuration_problem_not_a_media_one() -> None:
    """FFmpeg absent means fix the environment, not the video."""
    assert issubclass(ExternalToolNotFoundError, ConfigurationError)


@pytest.fixture
def workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.joinpath("workspace")
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(root))
    return root


def test_unknown_run_exits_with_invalid_input(workspace_env: Path) -> None:
    result = runner.invoke(cli.app, ["transcribe", "20260101T000000-absent-abc123"])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Run not found" in result.output


def test_unsafe_run_id_is_rejected(workspace_env: Path) -> None:
    result = runner.invoke(cli.app, ["transcribe", ".."])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Invalid run identifier" in result.output


def test_broken_profile_exits_with_configuration(tmp_path: Path, workspace_env: Path) -> None:
    profile = tmp_path.joinpath("bad.toml")
    profile.write_text('[transcription]\nmodle = "tiny"\n', encoding="utf-8")

    result = runner.invoke(cli.app, ["doctor", "--config", str(profile)])

    assert result.exit_code == EXIT_CONFIGURATION
    assert "transcription.modle" in result.output


def test_corrupt_manifest_reports_cleanly_without_a_traceback(
    tmp_path: Path, workspace_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = workspace_env.joinpath("runs", "20260101T000000-sample-abc123")
    run_path.joinpath("audio").mkdir(parents=True)
    run_path.joinpath("audio", "source.wav").write_bytes(b"wav")
    run_path.joinpath("manifest.json").write_text('{"run_id": "x"}', encoding="utf-8")

    result = runner.invoke(cli.app, ["transcribe", run_path.name])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Unsupported artifact schema" in result.output
    assert "Traceback" not in result.output
    assert "pydantic" not in result.output.lower()


def test_missing_audio_reports_the_missing_artifact(
    tmp_path: Path, workspace_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = workspace_env.joinpath("runs", "20260101T000000-sample-abc123")
    run_path.mkdir(parents=True)
    run_path.joinpath("manifest.json").write_text(
        json.dumps(_manifest_payload(run_path.name)), encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["transcribe", run_path.name])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Run audio is missing" in result.output


def _manifest_payload(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": "2026-01-01T00:00:00Z",
        "status": RunStatus.AUDIO_READY.value,
        "input": {"path": "/tmp/video.mp4", "sha256": "a" * 64, "size": 1},
        "config_sha256": "b" * 64,
        "versions": {
            "content_engine": "0.1.0",
            "python": "3.12.0",
            "ffmpeg": "ffmpeg version test",
            "transcription_model": "large-v3",
            "analysis_provider": "openai",
            "analysis_model": "SET_MODEL_HERE",
            "prompt_version": None,
            "prompt_sha256": None,
        },
        "stages": {},
        "failure": None,
    }


def test_a_failed_inspection_keeps_the_run_and_records_the_stage(
    tmp_path: Path, workspace_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path.joinpath("no-audio.mp4")
    video.write_bytes(b"video")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 320,
                "height": 240,
                "avg_frame_rate": "25/1",
            }
        ],
        "format": {"duration": "3.0", "format_name": "mov,mp4"},
    }

    def fake_probe(arguments: Sequence[str], timeout: float | None = None) -> Any:
        return fake_process(arguments, json.dumps(payload))

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_probe)
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )

    result = runner.invoke(cli.app, ["run", str(video)])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Missing audio stream" in result.output
    assert "kept for diagnosis" in result.output

    runs = list(workspace_env.joinpath("runs").iterdir())
    assert len(runs) == 1
    manifest = json.loads(runs[0].joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == RunStatus.FAILED_INSPECT.value
    assert manifest["failure"]["stage"] == RunStage.INSPECT.value
    assert manifest["failure"]["error_type"] == "NoAudioStreamError"
    assert "no audio stream" in manifest["failure"]["message"]


def test_a_transcription_provider_failure_is_not_an_analysis_failure() -> None:
    """The shell must not be told an analysis stage failed that never ran."""
    assert issubclass(TranscriptionProviderError, TranscriptionError)
    assert not issubclass(TranscriptionProviderError, ExternalProviderError)
    assert TranscriptionProviderError("x").exit_code == EXIT_TRANSCRIPTION
