from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from content_engine.config import Settings
from content_engine.domain.exceptions import ExternalToolNotFoundError
from content_engine.domain.models import MediaInfo
from content_engine.services.doctor_service import DoctorService
from content_engine.services.media_service import MediaService
from tests.conftest import fake_process


class FakeProbe:
    def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, object]]:
        return (
            MediaInfo(
                duration_seconds=10,
                video_codec="h264",
                width=1280,
                height=720,
                fps=30,
                audio_codec="aac",
                sample_rate=48000,
                channels=2,
                container="mp4",
                file_size=input_path.stat().st_size,
            ),
            {"probe": "raw"},
        )

    def probe_audio_duration(self, input_path: Path) -> float:
        return 42.5


class FakeFFmpeg:
    def __init__(self) -> None:
        self.arguments: tuple[Path, Path] | None = None

    def extract_audio(self, input_path: Path, output_path: Path) -> None:
        self.arguments = (input_path, output_path)


def test_media_service_inspects_and_extracts(tmp_path: Path) -> None:
    video = tmp_path.joinpath("video.mp4")
    video.write_bytes(b"video")
    probe_output = tmp_path.joinpath("run", "probe.json")
    ffmpeg = FakeFFmpeg()
    service = MediaService(FakeProbe(), ffmpeg)  # type: ignore[arg-type]

    media = service.inspect(video, probe_output)
    audio = tmp_path.joinpath("source.wav")
    service.extract_audio(video, audio)

    assert media.duration_seconds == 10
    assert probe_output.is_file()
    assert ffmpeg.arguments == (video, audio)


def test_media_service_reports_audio_duration(tmp_path: Path) -> None:
    audio = tmp_path.joinpath("source.wav")
    audio.write_bytes(b"wav")
    service = MediaService(FakeProbe(), FakeFFmpeg())  # type: ignore[arg-type]

    assert service.audio_duration(audio) == 42.5


def _healthy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(arguments: Sequence[str], timeout: float | None = None) -> Any:
        output = "ffmpeg version test\n"
        if "-filters" in arguments:
            output = " T.. ass Render ASS subtitles\n"
        return fake_process(arguments, output)

    monkeypatch.setattr("content_engine.services.doctor_service.run_command", fake_run)
    monkeypatch.setattr("content_engine.services.doctor_service.sys.version_info", (3, 12))
    monkeypatch.setattr(
        "content_engine.services.doctor_service.importlib.util.find_spec",
        lambda module: object(),
    )


def test_doctor_reports_ready_required_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    checks = DoctorService(settings).run()

    assert all(check.ok for check in checks if check.required)
    assert next(check for check in checks if check.name == "faster-whisper").ok


def test_doctor_reports_where_the_configuration_came_from(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration row must describe the layers, not assert success."""
    _healthy_environment(monkeypatch)
    profile = tmp_path.joinpath("profile.toml")

    detail = next(
        check.detail
        for check in DoctorService(settings, profile).run()
        if check.name == "Configuration"
    )

    assert "packaged content_engine.resources/default.toml" in detail
    assert str(profile) in detail


def test_doctor_flags_missing_tools(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(arguments: Sequence[str], timeout: float | None = None) -> Any:
        raise ExternalToolNotFoundError(f"{arguments[0]} was not found")

    monkeypatch.setattr("content_engine.services.doctor_service.run_command", missing)

    checks = {check.name: check for check in DoctorService(settings).run()}

    assert not checks["FFmpeg"].ok
    assert not checks["FFprobe"].ok
    assert not checks["ASS subtitles"].ok
    assert "was not found" in checks["FFmpeg"].detail


def test_doctor_flags_a_missing_ass_filter(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def without_ass(arguments: Sequence[str], timeout: float | None = None) -> Any:
        output = " T.. drawtext Draw text\n" if "-filters" in arguments else "ffmpeg version\n"
        return fake_process(arguments, output)

    monkeypatch.setattr("content_engine.services.doctor_service.run_command", without_ass)

    checks = {check.name: check for check in DoctorService(settings).run()}

    assert not checks["ASS subtitles"].ok
    assert checks["ASS subtitles"].detail == "filter unavailable"


def test_doctor_checks_that_runs_can_be_created(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_environment(monkeypatch)

    check = next(check for check in DoctorService(settings).run() if check.name == "Workspace")

    assert check.ok
    assert settings.workspace.root.joinpath("runs").is_dir()


def test_ai_configuration_is_optional_by_default(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_environment(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    checks = {check.name: check for check in DoctorService(settings).run()}

    assert not checks["OpenAI credentials"].required
    assert not checks["Analysis model"].required
    assert all(check.ok for check in checks.values() if check.required)


def test_require_ai_makes_credentials_and_model_mandatory(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy_environment(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    checks = {check.name: check for check in DoctorService(settings, require_ai=True).run()}

    assert checks["OpenAI credentials"].required
    assert not checks["OpenAI credentials"].ok
    assert checks["Analysis model"].required
    assert not checks["Analysis model"].ok
    assert "placeholder" in checks["Analysis model"].detail
