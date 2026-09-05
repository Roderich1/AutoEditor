import subprocess
from pathlib import Path

import pytest

from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import Settings
from content_engine.domain.enums import RunStatus
from content_engine.domain.models import MediaInfo
from content_engine.services.doctor_service import DoctorService
from content_engine.services.media_service import MediaService
from content_engine.services.run_service import RunService


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


def test_run_service_creates_reproducible_manifest(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path.joinpath("My Video.mp4")
    video.write_bytes(b"video-content")
    workspace = RunWorkspace(settings.workspace.root)

    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments: subprocess.CompletedProcess(arguments, 0, "ffmpeg version test\n", ""),
    )
    run_path, manifest = RunService(settings, workspace).create(video)

    assert manifest.run_id.startswith("20")
    assert "my-video" in manifest.run_id
    assert manifest.status == RunStatus.CREATED
    assert run_path.joinpath("config.effective.json").is_file()
    assert run_path.joinpath("manifest.json").is_file()

    RunService(settings, workspace).set_status(run_path, manifest, RunStatus.INSPECTED)
    assert workspace.read_manifest(run_path).status == RunStatus.INSPECTED


def test_doctor_reports_ready_required_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        output = "ffmpeg version test\n"
        if "-filters" in arguments:
            output = " T.. ass Render ASS subtitles\n"
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("content_engine.services.doctor_service.run_command", fake_run)
    monkeypatch.setattr("content_engine.services.doctor_service.sys.version_info", (3, 12))
    monkeypatch.setattr(
        "content_engine.services.doctor_service.importlib.util.find_spec",
        lambda module: object(),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    checks = DoctorService(settings).run()

    assert all(check.ok for check in checks if check.required)
    assert next(check for check in checks if check.name == "faster-whisper").ok
