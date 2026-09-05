import json
import subprocess
from pathlib import Path

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.domain.exceptions import NoAudioStreamError


def _probe_payload(include_audio: bool = True) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
        }
    ]
    if include_audio:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            }
        )
    return {
        "streams": streams,
        "format": {"duration": "12.5", "format_name": "mov,mp4"},
    }


def test_probe_parses_video_and_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path.joinpath("video.mp4")
    video.write_bytes(b"media")

    def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, json.dumps(_probe_payload()), "")

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_run)
    media, _ = FFprobeAdapter().probe(video)

    assert media.video_codec == "h264"
    assert media.audio_codec == "aac"
    assert media.duration_seconds == 12.5
    assert media.fps == pytest.approx(29.97, rel=1e-3)


def test_probe_rejects_media_without_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path.joinpath("video.bin")
    video.write_bytes(b"media")

    def fake_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, json.dumps(_probe_payload(False)), "")

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_run)
    with pytest.raises(NoAudioStreamError):
        FFprobeAdapter().probe(video)
