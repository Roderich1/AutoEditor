"""Fixtures backed by real FFmpeg.

Every fixture is synthesised locally with lavfi. Nothing is downloaded, nothing
is committed and no network is touched.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="FFmpeg and ffprobe are not installed; install them to run integration tests",
)


def _render(arguments: list[str]) -> None:
    assert FFMPEG is not None
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(scope="session")
def media_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample_video(media_root: Path) -> Path:
    """A three second 320x240 clip with a sine tone on one audio track."""
    path = media_root.joinpath("sample.mp4")
    if not path.is_file():
        _render(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=25:duration=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ]
        )
    return path


@pytest.fixture(scope="session")
def video_without_audio(media_root: Path) -> Path:
    path = media_root.joinpath("no-audio.mp4")
    if not path.is_file():
        _render(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=25:duration=2",
                "-c:v",
                "libx264",
                str(path),
            ]
        )
    return path


@pytest.fixture(scope="session")
def corrupt_video(media_root: Path) -> Path:
    path = media_root.joinpath("corrupt.mp4")
    path.write_bytes(b"this is not a container")
    return path


@pytest.fixture(scope="session")
def probe_json() -> Callable[[Path], dict]:
    """Independent ffprobe reading, used to verify what the adapter produced."""
    import json

    def probe(path: Path) -> dict:
        assert FFPROBE is not None
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        return dict(json.loads(result.stdout))

    return probe
