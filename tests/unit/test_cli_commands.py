"""doctor, inspect and run at the command boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.config import WORKSPACE_ENV_VAR
from content_engine.domain.exceptions import EXIT_CONFIGURATION, EXIT_INVALID_INPUT
from tests.conftest import fake_process

runner = CliRunner()

VIDEO_PROBE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 320,
            "height": 240,
            "avg_frame_rate": "25/1",
        },
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
    ],
    "format": {"duration": "3.0", "format_name": "mov,mp4"},
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.joinpath("workspace")
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(root))
    return root


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("sample.mp4")
    path.write_bytes(b"video")
    return path


def _stub_media(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] = VIDEO_PROBE) -> None:
    def fake_probe(arguments: Sequence[str], timeout: float | None = None) -> Any:
        return fake_process(arguments, json.dumps(payload))

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_probe)
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )
    monkeypatch.setattr(
        "content_engine.adapters.media.ffmpeg.run_command",
        lambda arguments, **_: _write_wav(arguments),
    )


def _write_wav(arguments: Sequence[str]) -> Any:
    Path(arguments[-1]).write_bytes(b"wav-bytes")
    return fake_process(arguments)


def _healthy_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(arguments: Sequence[str], timeout: float | None = None) -> Any:
        output = " T.. ass Render ASS subtitles\n" if "-filters" in arguments else "ffmpeg v\n"
        return fake_process(arguments, output)

    monkeypatch.setattr("content_engine.services.doctor_service.run_command", fake_run)
    monkeypatch.setattr("content_engine.services.doctor_service.sys.version_info", (3, 12))
    monkeypatch.setattr(
        "content_engine.services.doctor_service.importlib.util.find_spec",
        lambda module: object(),
    )


def test_doctor_reports_ready(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy_doctor(monkeypatch)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "System ready" in result.output


def test_doctor_fails_with_the_configuration_exit_code(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken environment is a configuration problem, not an unknown crash."""
    _healthy_doctor(monkeypatch)
    monkeypatch.setattr("content_engine.services.doctor_service.sys.version_info", (3, 11))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == EXIT_CONFIGURATION
    assert "Environment is not ready" in result.output


def test_doctor_require_ai_fails_without_credentials(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_doctor(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert runner.invoke(cli.app, ["doctor"]).exit_code == 0
    assert runner.invoke(cli.app, ["doctor", "--require-ai"]).exit_code == EXIT_CONFIGURATION


def test_inspect_prints_media_information(video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_media(monkeypatch)

    result = runner.invoke(cli.app, ["inspect", str(video)])

    assert result.exit_code == 0
    assert '"video_codec": "h264"' in result.output


def test_inspect_rejects_media_without_audio(video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "streams": [s for s in VIDEO_PROBE["streams"] if s["codec_type"] != "audio"],
        "format": VIDEO_PROBE["format"],
    }
    _stub_media(monkeypatch, payload)

    result = runner.invoke(cli.app, ["inspect", str(video)])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Missing audio stream" in result.output


def test_run_creates_a_run_and_reaches_audio_ready(
    workspace: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_media(monkeypatch)

    result = runner.invoke(cli.app, ["run", str(video)])

    assert result.exit_code == 0
    assert "Run ready" in result.output
    runs = list(workspace.joinpath("runs").iterdir())
    assert len(runs) == 1
    manifest = json.loads(runs[0].joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "AUDIO_READY"
    assert runs[0].joinpath("media", "probe.json").is_file()
    assert runs[0].joinpath("audio", "source.wav").is_file()


def test_each_run_invocation_creates_a_new_run(
    workspace: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency for run arrives with CE-047 to CE-052; today it always creates."""
    _stub_media(monkeypatch)

    runner.invoke(cli.app, ["run", str(video)])
    runner.invoke(cli.app, ["run", str(video)])

    assert len(list(workspace.joinpath("runs").iterdir())) == 2


def test_audio_extraction_failure_is_recorded_against_the_audio_stage(
    workspace: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from content_engine.domain.exceptions import ExternalToolError

    _stub_media(monkeypatch)

    def failing(arguments: Sequence[str], timeout: float | None = None) -> Any:
        raise ExternalToolError("ffmpeg failed: encoder not found")

    monkeypatch.setattr("content_engine.adapters.media.ffmpeg.run_command", failing)

    result = runner.invoke(cli.app, ["run", str(video)])

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "kept for diagnosis" in result.output
    runs = list(workspace.joinpath("runs").iterdir())
    manifest = json.loads(runs[0].joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED_AUDIO"
    assert manifest["failure"]["stage"] == "audio"


def test_an_unexpected_error_is_reported_without_a_traceback(
    video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug must still produce a readable message and exit 1, not a stack dump."""
    from content_engine.domain.exceptions import EXIT_UNKNOWN

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", explode)

    result = runner.invoke(cli.app, ["inspect", str(video)])

    assert result.exit_code == EXIT_UNKNOWN
    assert "Unexpected error: RuntimeError" in result.output
    assert "Traceback" not in result.output
