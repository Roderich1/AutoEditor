from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.domain.exceptions import (
    ExternalToolError,
    ExternalToolNotFoundError,
    InvalidMediaError,
    NoAudioStreamError,
)
from tests.conftest import fake_process


def _probe_payload(
    include_audio: bool = True,
    include_video: bool = True,
    duration: str | None = "12.5",
    avg_frame_rate: str = "30000/1001",
    r_frame_rate: str = "30000/1001",
) -> dict[str, Any]:
    streams: list[dict[str, object]] = []
    if include_video:
        streams.append(
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": avg_frame_rate,
                "r_frame_rate": r_frame_rate,
            }
        )
    if include_audio:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            }
        )
    format_info: dict[str, object] = {"format_name": "mov,mp4"}
    if duration is not None:
        format_info["duration"] = duration
    return {"streams": streams, "format": format_info}


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("video.mp4")
    path.write_bytes(b"media")
    return path


def _stub(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    def fake_run(arguments: Sequence[str], timeout: float | None = None) -> Any:
        return fake_process(arguments, json.dumps(payload))

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_run)


def _raise(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fake_run(arguments: Sequence[str], timeout: float | None = None) -> Any:
        raise error

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_run)


def test_probe_parses_video_and_audio(media_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, _probe_payload())

    media, raw = FFprobeAdapter().probe(media_file)

    assert media.video_codec == "h264"
    assert media.audio_codec == "aac"
    assert media.duration_seconds == 12.5
    assert media.fps == pytest.approx(29.97, rel=1e-3)
    assert raw["format"]["format_name"] == "mov,mp4"


def test_probe_rejects_media_without_audio(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(include_audio=False))

    adapter = FFprobeAdapter()

    with pytest.raises(NoAudioStreamError, match="no audio stream"):
        adapter.probe(media_file)


def test_probe_rejects_media_without_video(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(include_video=False))

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="no video stream"):
        adapter.probe(media_file)


def test_probe_rejects_non_positive_duration(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(duration="0"))

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="duration is not positive"):
        adapter.probe(media_file)


def test_probe_rejects_missing_duration(media_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, _probe_payload(duration=None))

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="does not declare a duration"):
        adapter.probe(media_file)


def test_unreadable_media_is_invalid_media_not_a_provider_failure(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt file is a problem with the input, not with the tool."""
    _raise(monkeypatch, ExternalToolError("ffprobe failed: moov atom not found"))

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="ffprobe could not read"):
        adapter.probe(media_file)


def test_missing_ffprobe_stays_a_configuration_problem(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raise(monkeypatch, ExternalToolNotFoundError("ffprobe was not found"))

    adapter = FFprobeAdapter()

    with pytest.raises(ExternalToolNotFoundError):
        adapter.probe(media_file)


def test_invalid_json_response_is_rejected(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(arguments: Sequence[str], timeout: float | None = None) -> Any:
        return fake_process(arguments, "not json")

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_run)

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="Invalid ffprobe response"):
        adapter.probe(media_file)


def test_missing_file_is_rejected_before_running_ffprobe(tmp_path: Path) -> None:
    adapter = FFprobeAdapter()
    absent = tmp_path.joinpath("absent.mp4")

    with pytest.raises(InvalidMediaError, match="does not exist"):
        adapter.probe(absent)


def test_unknown_frame_rate_falls_back_then_degrades_to_zero(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undeclared frame rate must not reject an otherwise valid source."""
    _stub(monkeypatch, _probe_payload(avg_frame_rate="0/0", r_frame_rate="25/1"))
    assert FFprobeAdapter().probe(media_file)[0].fps == 25.0

    _stub(monkeypatch, _probe_payload(avg_frame_rate="0/0", r_frame_rate="0/0"))
    assert FFprobeAdapter().probe(media_file)[0].fps == 0.0


def test_probe_audio_duration_requires_an_audio_stream(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(include_audio=False))

    adapter = FFprobeAdapter()

    with pytest.raises(NoAudioStreamError):
        adapter.probe_audio_duration(media_file)


def test_probe_audio_duration_returns_seconds(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(duration="3.25"))

    assert FFprobeAdapter().probe_audio_duration(media_file) == 3.25


def test_incomplete_stream_metadata_is_rejected(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _probe_payload()
    del payload["streams"][0]["width"]
    _stub(monkeypatch, payload)

    adapter = FFprobeAdapter()

    with pytest.raises(InvalidMediaError, match="Incomplete media metadata"):
        adapter.probe(media_file)


def test_an_unparseable_frame_rate_degrades_to_zero(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, _probe_payload(avg_frame_rate="not-a-fraction", r_frame_rate=""))

    assert FFprobeAdapter().probe(media_file)[0].fps == 0.0
