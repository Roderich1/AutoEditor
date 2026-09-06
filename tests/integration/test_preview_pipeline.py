"""CE-034 against real FFmpeg and real ffprobe.

The unit suite proves which arguments are built. This proves those arguments
produce a file a player can open: an MP4 at the configured size, holding the
requested seconds of video and the audio that was in the source. Every fixture
is synthesised locally with lavfi, so nothing is downloaded and no external
video is involved.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from content_engine.adapters.media.preview import FFmpegPreviewRenderer
from content_engine.domain.preview_rules import (
    PREVIEW_DURATION_TOLERANCE_SECONDS,
    PREVIEW_INDEX_FILENAME,
    preview_filename,
    preview_stage_config,
)
from content_engine.services.preview_service import (
    PreviewPlan,
    PreviewService,
    read_index,
    verify_previews,
)

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.domain.candidates import CandidateCollection
from content_engine.domain.exceptions import RenderError
from tests.conftest import chunk_of, collect, raw_candidate, speech_transcript
from tests.integration.conftest import requires_ffmpeg

pytestmark = [pytest.mark.integration, requires_ffmpeg]

GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
SOURCE_SHA = "c" * 64


@pytest.fixture(scope="module")
def wide_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Forty seconds of 640x360 colour bars with a tone, in a directory with a ñ."""
    directory = tmp_path_factory.mktemp("clases de ñandú")
    path = directory.joinpath("mi vídeo de prueba.mp4")
    if not path.is_file():
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=25:duration=40",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=40",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    return path


def shortlist() -> CandidateCollection:
    """Two candidates inside the fixture, both clearing the 20 second minimum."""
    transcript = speech_transcript(count=4)
    return collect(
        chunk_of(transcript),
        [raw_candidate(2.0, 24.0), raw_candidate(16.0, 38.0, hook=88)],
        source_duration_seconds=40.0,
    )


def plan_for(source: Path, collection: CandidateCollection) -> PreviewPlan:
    return PreviewPlan(
        candidates=tuple(collection.candidates),
        config=preview_stage_config(width=540, height=960),
        analysis_fingerprint=FINGERPRINT,
        source_path=source,
        source_sha256=SOURCE_SHA,
        source_duration_seconds=collection.source_duration_seconds,
    )


@pytest.fixture
def generated(wide_source: Path, tmp_path: Path) -> tuple[Path, PreviewPlan, str, str]:
    directory = tmp_path.joinpath("previews")
    collection = shortlist()
    assert len(collection.candidates) == 2
    plan = plan_for(wide_source, collection)
    outcome = PreviewService(FFmpegPreviewRenderer(), FFprobeAdapter()).generate(
        plan, directory, GENERATED_AT
    )
    return directory, plan, outcome.fingerprint, outcome.stage_config_sha256


class TestRealPreviews:
    def test_one_playable_file_per_candidate(
        self,
        generated: tuple[Path, PreviewPlan, str, str],
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        directory, plan, _, _ = generated
        for candidate in plan.candidates:
            path = directory.joinpath(preview_filename(candidate.id))
            assert path.is_file()
            assert path.stat().st_size > 0
            raw = probe_json(path)
            streams = {stream["codec_type"]: stream for stream in raw["streams"]}
            assert streams["video"]["codec_name"] == "h264"
            assert streams["audio"]["codec_name"] == "aac"

    def test_the_frame_is_exactly_the_configured_size(
        self,
        generated: tuple[Path, PreviewPlan, str, str],
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        directory, plan, _, _ = generated
        for candidate in plan.candidates:
            raw = probe_json(directory.joinpath(preview_filename(candidate.id)))
            video = next(item for item in raw["streams"] if item["codec_type"] == "video")
            assert (video["width"], video["height"]) == (540, 960)

    def test_the_image_is_not_stretched(
        self,
        generated: tuple[Path, PreviewPlan, str, str],
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        """A 640x360 source fitted into 540x960 keeps square pixels and pads."""
        directory, plan, _, _ = generated
        raw = probe_json(directory.joinpath(preview_filename(plan.candidates[0].id)))
        video = next(item for item in raw["streams"] if item["codec_type"] == "video")
        assert video.get("sample_aspect_ratio", "1:1") == "1:1"

    def test_the_duration_matches_the_candidate_within_tolerance(
        self,
        generated: tuple[Path, PreviewPlan, str, str],
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        directory, plan, _, _ = generated
        for candidate in plan.candidates:
            raw = probe_json(directory.joinpath(preview_filename(candidate.id)))
            measured = float(raw["format"]["duration"])
            assert abs(measured - candidate.duration) <= PREVIEW_DURATION_TOLERANCE_SECONDS

    def test_the_index_describes_what_is_on_disk(
        self, generated: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        directory, plan, _, _ = generated
        index = read_index(directory)
        assert len(index.previews) == len(plan.candidates)
        for entry in index.previews:
            path = directory.joinpath(entry.filename)
            assert path.stat().st_size == entry.size_bytes

    def test_the_index_is_valid_json_with_lf_endings(
        self, generated: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        directory, _, _, _ = generated
        raw = directory.joinpath(PREVIEW_INDEX_FILENAME).read_bytes()
        assert b"\r\n" not in raw
        assert json.loads(raw.decode("utf-8"))["previews"]

    def test_nothing_temporary_is_left_behind(
        self, generated: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        directory, _, _, _ = generated
        assert [path.name for path in directory.iterdir() if path.is_dir()] == []
        assert list(directory.rglob("*.tmp")) == []

    def test_a_finished_set_verifies_and_is_reused_untouched(
        self, generated: tuple[Path, PreviewPlan, str, str]
    ) -> None:
        directory, plan, fingerprint, digest = generated
        before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
        verify_previews(directory, fingerprint, digest, plan)
        assert {path.name: path.read_bytes() for path in sorted(directory.iterdir())} == before

    def test_a_source_ffmpeg_cannot_read_fails_without_artifacts(self, tmp_path: Path) -> None:
        broken = tmp_path.joinpath("no es un contenedor.mp4")
        broken.write_bytes(b"this is not a container")
        directory = tmp_path.joinpath("previews")
        with pytest.raises(RenderError):
            PreviewService(FFmpegPreviewRenderer(), FFprobeAdapter()).generate(
                plan_for(broken, shortlist()), directory, GENERATED_AT
            )
        survivors = sorted(path.name for path in directory.rglob("*")) if directory.exists() else []
        assert survivors == []
