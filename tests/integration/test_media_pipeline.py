"""CE-015. Media inspection and audio extraction against real FFmpeg."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import Settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import InvalidMediaError, NoAudioStreamError
from content_engine.services.media_service import MediaService
from content_engine.services.run_service import RunService
from tests.integration.conftest import requires_ffmpeg

pytestmark = [pytest.mark.integration, requires_ffmpeg]

EXPECTED_DURATION = 3.0
DURATION_TOLERANCE = 0.2


@pytest.fixture
def media_service() -> MediaService:
    return MediaService(FFprobeAdapter(), FFmpegAdapter())


class TestInspection:
    def test_a_valid_mp4_is_described_accurately(
        self, media_service: MediaService, sample_video: Path
    ) -> None:
        media = media_service.inspect(sample_video)

        assert media.duration_seconds == pytest.approx(EXPECTED_DURATION, abs=DURATION_TOLERANCE)
        assert media.video_codec == "h264"
        assert (media.width, media.height) == (320, 240)
        assert media.fps == pytest.approx(25.0, abs=0.01)
        assert media.audio_codec == "aac"
        assert media.file_size == sample_video.stat().st_size

    def test_the_raw_probe_is_persisted(
        self, media_service: MediaService, sample_video: Path, tmp_path: Path
    ) -> None:
        probe_output = tmp_path.joinpath("media", "probe.json")

        media_service.inspect(sample_video, probe_output)

        payload = json.loads(probe_output.read_text(encoding="utf-8"))
        assert {stream["codec_type"] for stream in payload["streams"]} == {"video", "audio"}
        assert b"\r\n" not in probe_output.read_bytes()

    def test_a_video_without_audio_is_rejected(
        self, media_service: MediaService, video_without_audio: Path
    ) -> None:
        with pytest.raises(NoAudioStreamError):
            media_service.inspect(video_without_audio)

    def test_a_corrupt_file_is_rejected_as_invalid_media(
        self, media_service: MediaService, corrupt_video: Path
    ) -> None:
        """ffprobe fails, and that is reported as bad input rather than a tool fault."""
        with pytest.raises(InvalidMediaError, match="ffprobe could not read"):
            media_service.inspect(corrupt_video)

    def test_the_extension_is_not_trusted(
        self, media_service: MediaService, tmp_path: Path, sample_video: Path
    ) -> None:
        disguised = tmp_path.joinpath("actually-a-video.txt")
        disguised.write_bytes(sample_video.read_bytes())

        assert media_service.inspect(disguised).video_codec == "h264"


class TestAudioExtraction:
    @pytest.fixture
    def extracted(self, media_service: MediaService, sample_video: Path, tmp_path: Path) -> Path:
        output = tmp_path.joinpath("audio", "source.wav")
        media_service.extract_audio(sample_video, output)
        return output

    def test_the_wav_exists_and_is_not_empty(self, extracted: Path) -> None:
        assert extracted.is_file()
        assert extracted.stat().st_size > 0

    def test_the_wav_is_mono_16khz_pcm_s16le(
        self, extracted: Path, probe_json: Callable[[Path], dict]
    ) -> None:
        stream = probe_json(extracted)["streams"][0]

        assert stream["codec_type"] == "audio"
        assert stream["codec_name"] == "pcm_s16le"
        assert int(stream["sample_rate"]) == 16000
        assert int(stream["channels"]) == 1

    def test_the_wav_duration_matches_the_source(
        self, extracted: Path, probe_json: Callable[[Path], dict]
    ) -> None:
        duration = float(probe_json(extracted)["format"]["duration"])

        assert duration == pytest.approx(EXPECTED_DURATION, abs=DURATION_TOLERANCE)

    def test_the_adapter_measures_the_same_duration(
        self, media_service: MediaService, extracted: Path
    ) -> None:
        assert media_service.audio_duration(extracted) == pytest.approx(
            EXPECTED_DURATION, abs=DURATION_TOLERANCE
        )

    def test_extraction_refuses_a_source_without_audio(
        self, media_service: MediaService, video_without_audio: Path, tmp_path: Path
    ) -> None:
        from content_engine.domain.exceptions import AudioExtractionError

        output = tmp_path.joinpath("out.wav")

        with pytest.raises(AudioExtractionError):
            media_service.extract_audio(video_without_audio, output)


class TestRunLifecycle:
    def test_a_valid_source_reaches_audio_ready(
        self, settings: Settings, sample_video: Path, media_service: MediaService
    ) -> None:
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path, manifest = run_service.create(sample_video)

        media_service.inspect(sample_video, run_path.joinpath("media", "probe.json"))
        manifest = run_service.advance(run_path, manifest, RunStatus.INSPECTED)
        media_service.extract_audio(sample_video, run_path.joinpath("audio", "source.wav"))
        run_service.advance(run_path, manifest, RunStatus.AUDIO_READY)

        stored = workspace.read_manifest(run_path)
        assert stored.status == RunStatus.AUDIO_READY
        assert stored.failure is None
        assert stored.versions.ffmpeg.startswith("ffmpeg version")
        assert run_path.joinpath("audio", "source.wav").is_file()

    def test_an_invalid_source_leaves_a_diagnosable_run(
        self, settings: Settings, video_without_audio: Path, media_service: MediaService
    ) -> None:
        workspace = RunWorkspace(settings.workspace.root)
        run_service = RunService(settings, workspace)
        run_path, manifest = run_service.create(video_without_audio)

        probe_output = run_path.joinpath("media", "probe.json")

        with pytest.raises(NoAudioStreamError) as error:
            media_service.inspect(video_without_audio, probe_output)
        run_service.fail(run_path, manifest, RunStage.INSPECT, error.value)

        stored = workspace.read_manifest(run_path)
        assert stored.status == RunStatus.FAILED_INSPECT
        assert stored.failure is not None
        assert stored.failure.stage == RunStage.INSPECT
        assert run_path.is_dir()


def test_extraction_reports_an_empty_output(
    media_service: MediaService, sample_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FFmpeg exiting cleanly is not proof that it wrote usable audio."""
    from content_engine.domain.exceptions import AudioExtractionError
    from tests.conftest import fake_process

    output = tmp_path.joinpath("empty.wav")

    def write_nothing(arguments: Any, timeout: float | None = None) -> Any:
        output.write_bytes(b"")
        return fake_process(arguments)

    monkeypatch.setattr("content_engine.adapters.media.ffmpeg.run_command", write_nothing)

    with pytest.raises(AudioExtractionError, match="produced no audio"):
        media_service.extract_audio(sample_video, output)
