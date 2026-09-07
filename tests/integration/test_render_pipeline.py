"""CE-040 to CE-046 against real FFmpeg and real ffprobe.

The unit suite proves which arguments are built. This proves those arguments
produce something a player can open: a 1080x1920 H.264/AAC file with square
pixels, holding the requested seconds, with the subtitles burned into the
picture and the two sidecar documents beside it.

Both presets are exercised, on sources whose shapes are the ones that break a
naive filter graph: a wide 16:9 recording, a source that is already vertical,
and one with odd dimensions that ``yuv420p`` cannot represent unless the scaler
was told to keep them even. The fixtures are synthesised locally with lavfi and
live under a directory whose name has a space and an ``ñ``, because the ASS
filter takes its path as an option value inside a filter graph and that is where
a Windows drive letter and a space go wrong.

Nothing is downloaded, nothing is committed, no network is touched and no
credential is read.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.media.render import FFmpegClipRenderer
from content_engine.config import load_settings
from content_engine.domain.candidates import CandidateCollection
from content_engine.domain.enums import RenderPreset
from content_engine.domain.exceptions import RenderError
from content_engine.domain.models import Transcript
from content_engine.domain.render_rules import (
    RENDER_INDEX_FILENAME,
    render_stage_config,
)
from content_engine.domain.render_targets import RenderTarget, build_render_target
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    RENDER_DURATION_TOLERANCE_SECONDS,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
    RenderStageConfig,
)
from content_engine.domain.review import ApprovedDecision, ReviewDecisionCollection
from content_engine.domain.subtitles import read_ass_events, read_srt_events
from content_engine.services.render_service import (
    RenderPlan,
    RenderService,
    read_index,
    verify_clips,
)
from tests.conftest import chunk_of, collect, raw_candidate, speech_transcript
from tests.integration.conftest import requires_ffmpeg

pytestmark = [pytest.mark.integration, requires_ffmpeg]

GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _synthesise(path: Path, size: str, seconds: int, pixel_format: str = "yuv420p") -> Path:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
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
                f"testsrc=size={size}:rate=25:duration={seconds}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                pixel_format,
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


@pytest.fixture(scope="module")
def awkward_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory whose name has a space and a non-ASCII character.

    Not decoration. The ASS filter's filename is an option value inside a
    filtergraph, so it passes through two levels of unescaping, and a space, an
    accent and a Windows drive colon are exactly what breaks there.
    """
    return tmp_path_factory.mktemp("clases de ñandú")


@pytest.fixture(scope="module")
def wide_source(awkward_root: Path) -> Path:
    """Forty seconds of 640x360, the shape of a screen recording."""
    return _synthesise(awkward_root.joinpath("mi vídeo ancho.mp4"), "640x360", 40)


@pytest.fixture(scope="module")
def tall_source(awkward_root: Path) -> Path:
    """Already vertical, so the blur preset has almost nothing to fill."""
    return _synthesise(awkward_root.joinpath("mi vídeo vertical.mp4"), "608x1080", 40)


@pytest.fixture(scope="module")
def odd_source(awkward_root: Path) -> Path:
    """Odd dimensions, which is why it is 4:4:4.

    ``yuv420p`` subsamples chroma by two in each direction and cannot represent
    an odd width or height at all, so a source like this can only exist in a
    format that does not subsample. That is the whole reason the render's
    scalers carry ``force_divisible_by=2``: fitting 641x361 into a 9:16 frame
    lands on an odd intermediate size, and the encode into ``yuv420p`` would be
    refused by the same rule that makes this fixture unusual.
    """
    return _synthesise(awkward_root.joinpath("raro 641x361.mp4"), "641x361", 40, "yuv444p")


def transcript_of() -> Transcript:
    return speech_transcript(count=4)


def shortlist(transcript: Transcript) -> CandidateCollection:
    """Two candidates inside the fixture, both clearing the 20 second minimum."""
    return collect(
        chunk_of(transcript),
        [raw_candidate(2.0, 24.0), raw_candidate(16.0, 38.0, hook=88)],
        source_duration_seconds=40.0,
    )


def target_of(transcript: Transcript, collection: CandidateCollection) -> RenderTarget:
    decisions = ReviewDecisionCollection(
        analysis_fingerprint=DIGEST,
        source_duration_seconds=collection.source_duration_seconds,
        created_at=GENERATED_AT,
        updated_at=GENERATED_AT,
        decisions=[
            ApprovedDecision(
                candidate_id=candidate.id,
                original_start=candidate.start,
                original_end=candidate.end,
                reviewed_at=GENERATED_AT,
                final_start=candidate.start,
                final_end=candidate.end,
            )
            for candidate in collection.candidates
        ],
    )
    return build_render_target(
        candidates=tuple(collection.candidates),
        decisions=decisions,
        analysis_fingerprint=DIGEST,
        review_fingerprint="b" * 64,
        decisions_sha256="c" * 64,
        transcript_sha256="d" * 64,
        source_sha256="e" * 64,
        source_duration_seconds=collection.source_duration_seconds,
    )


def config_of(**overrides: Any) -> RenderStageConfig:
    built = render_stage_config(load_settings().render)
    if not overrides:
        return built
    return RenderStageConfig.model_validate(built.model_dump(mode="json") | overrides)


def plan_for(source: Path, **overrides: Any) -> RenderPlan:
    transcript = transcript_of()
    collection = shortlist(transcript)
    assert len(collection.candidates) == 2
    return RenderPlan(
        target=target_of(transcript, collection),
        config=config_of(**overrides),
        source_path=source,
        transcript=transcript,
        run_id="integration",
    )


def render(plan: RenderPlan, directory: Path) -> Any:
    return RenderService(FFmpegClipRenderer(), FFprobeAdapter()).generate(
        plan, directory, GENERATED_AT
    )


@pytest.fixture(scope="module")
def rendered(wide_source: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any, Any]:
    """One blur render of the wide source, shared by the assertions about it."""
    directory = tmp_path_factory.mktemp("salida con espacios").joinpath("clips")
    plan = plan_for(wide_source)
    return directory, plan, render(plan, directory)


def video_stream(raw: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in raw["streams"] if item["codec_type"] == "video")


def audio_stream(raw: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in raw["streams"] if item["codec_type"] == "audio")


class TestTheFinishedClip:
    def test_every_clip_is_a_playable_h264_aac_file(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        assert len(outcome.index.clips) == 2
        for clip in outcome.index.clips:
            path = directory.joinpath(clip.directory, CLIP_FILENAME)
            assert path.stat().st_size > 0
            raw = probe_json(path)
            assert video_stream(raw)["codec_name"] == "h264"
            assert audio_stream(raw)["codec_name"] == "aac"

    def test_every_clip_is_1080x1920_with_square_pixels(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            raw = probe_json(directory.joinpath(clip.directory, CLIP_FILENAME))
            video = video_stream(raw)
            assert (video["width"], video["height"]) == (1080, 1920)
            assert video["sample_aspect_ratio"] == "1:1"

    def test_every_duration_is_within_the_documented_tolerance(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            raw = probe_json(directory.joinpath(clip.directory, CLIP_FILENAME))
            drift = abs(float(raw["format"]["duration"]) - clip.duration)
            assert drift <= RENDER_DURATION_TOLERANCE_SECONDS

    def test_audio_is_present_in_every_clip(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            raw = probe_json(directory.joinpath(clip.directory, CLIP_FILENAME))
            assert int(audio_stream(raw)["channels"]) >= 1

    def test_no_subtitle_or_data_track_is_carried_into_the_clip(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            raw = probe_json(directory.joinpath(clip.directory, CLIP_FILENAME))
            kinds = sorted(stream["codec_type"] for stream in raw["streams"])
            assert kinds == ["audio", "video"]

    def test_each_directory_holds_the_four_artifacts(
        self, rendered: tuple[Path, RenderPlan, Any]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            names = sorted(path.name for path in directory.joinpath(clip.directory).iterdir())
            assert names == sorted(
                (
                    CLIP_FILENAME,
                    CLIP_METADATA_FILENAME,
                    SUBTITLES_ASS_FILENAME,
                    SUBTITLES_SRT_FILENAME,
                )
            )


class TestSubtitleFiles:
    def test_both_documents_parse_and_agree(self, rendered: tuple[Path, RenderPlan, Any]) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            base = directory.joinpath(clip.directory)
            srt = read_srt_events(base.joinpath(SUBTITLES_SRT_FILENAME).read_text("utf-8"))
            ass = read_ass_events(base.joinpath(SUBTITLES_ASS_FILENAME).read_text("utf-8"))
            assert len(srt) == clip.cue_count
            assert [event.text for event in srt] == [event.text for event in ass]

    def test_every_event_lies_inside_the_clip(self, rendered: tuple[Path, RenderPlan, Any]) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            base = directory.joinpath(clip.directory)
            limit = round(clip.duration * 1000)
            for name, parse in (
                (SUBTITLES_SRT_FILENAME, read_srt_events),
                (SUBTITLES_ASS_FILENAME, read_ass_events),
            ):
                previous = 0
                for event in parse(base.joinpath(name).read_text("utf-8")):
                    assert 0 <= event.start_ms <= limit
                    assert event.start_ms >= previous
                    assert event.end_ms <= limit
                    previous = event.end_ms

    def test_the_files_are_utf8_without_a_bom_and_lf_only(
        self, rendered: tuple[Path, RenderPlan, Any]
    ) -> None:
        directory, _, outcome = rendered
        for clip in outcome.index.clips:
            base = directory.joinpath(clip.directory)
            for name in (SUBTITLES_SRT_FILENAME, SUBTITLES_ASS_FILENAME):
                raw = base.joinpath(name).read_bytes()
                assert not raw.startswith(b"\xef\xbb\xbf")
                assert b"\r" not in raw
                raw.decode("utf-8")

    def test_ffmpeg_really_read_the_ass_file_from_an_awkward_path(
        self, wide_source: Path, tmp_path: Path
    ) -> None:
        """The burn is not silently skipped when the path needs escaping.

        FFmpeg fails the whole graph when ``ass=`` cannot open its file, so a
        clip that exists at all is proof the escaped path resolved. That is the
        assertion available without comparing pixels, and it is the one that
        would have caught an unescaped Windows drive colon.
        """
        directory = tmp_path.joinpath("mis salidas ñ", "clips")
        outcome = render(plan_for(wide_source), directory)

        assert len(outcome.index.clips) == 2
        assert all(clip.subtitles_burned for clip in outcome.index.clips)


class TestPresets:
    def test_vertical_blur_keeps_the_whole_wide_frame(
        self, rendered: tuple[Path, RenderPlan, Any], probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory, _, outcome = rendered
        raw = probe_json(directory.joinpath(outcome.index.clips[0].directory, CLIP_FILENAME))
        assert (video_stream(raw)["width"], video_stream(raw)["height"]) == (1080, 1920)

    def test_vertical_crop_produces_the_same_frame(
        self, wide_source: Path, tmp_path: Path, probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory = tmp_path.joinpath("clips")
        plan = plan_for(wide_source, preset=RenderPreset.VERTICAL_CROP.value)

        outcome = render(plan, directory)

        assert outcome.index.preset is RenderPreset.VERTICAL_CROP
        for clip in outcome.index.clips:
            raw = probe_json(directory.joinpath(clip.directory, CLIP_FILENAME))
            video = video_stream(raw)
            assert (video["width"], video["height"]) == (1080, 1920)
            assert video["sample_aspect_ratio"] == "1:1"

    @pytest.mark.parametrize(
        "preset", [RenderPreset.VERTICAL_BLUR.value, RenderPreset.VERTICAL_CROP.value]
    )
    def test_an_already_vertical_source_renders(
        self,
        tall_source: Path,
        tmp_path: Path,
        preset: str,
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        directory = tmp_path.joinpath(preset)

        outcome = render(plan_for(tall_source, preset=preset), directory)

        raw = probe_json(directory.joinpath(outcome.index.clips[0].directory, CLIP_FILENAME))
        assert (video_stream(raw)["width"], video_stream(raw)["height"]) == (1080, 1920)

    @pytest.mark.parametrize(
        "preset", [RenderPreset.VERTICAL_BLUR.value, RenderPreset.VERTICAL_CROP.value]
    )
    def test_an_odd_sized_source_renders(
        self,
        odd_source: Path,
        tmp_path: Path,
        preset: str,
        probe_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        """641x361 into yuv420p is the case force_divisible_by=2 exists for."""
        directory = tmp_path.joinpath(preset)

        outcome = render(plan_for(odd_source, preset=preset), directory)

        raw = probe_json(directory.joinpath(outcome.index.clips[0].directory, CLIP_FILENAME))
        assert (video_stream(raw)["width"], video_stream(raw)["height"]) == (1080, 1920)

    def test_burning_can_be_switched_off(
        self, wide_source: Path, tmp_path: Path, probe_json: Callable[[Path], dict[str, Any]]
    ) -> None:
        directory = tmp_path.joinpath("clips")

        outcome = render(plan_for(wide_source, burn_subtitles=False), directory)

        assert all(clip.subtitles_burned is False for clip in outcome.index.clips)
        for clip in outcome.index.clips:
            base = directory.joinpath(clip.directory)
            assert base.joinpath(SUBTITLES_SRT_FILENAME).is_file()
            assert base.joinpath(SUBTITLES_ASS_FILENAME).is_file()
            raw = probe_json(base.joinpath(CLIP_FILENAME))
            assert (video_stream(raw)["width"], video_stream(raw)["height"]) == (1080, 1920)


class TestTheStageArtifacts:
    def test_the_index_describes_what_is_on_disk(
        self, rendered: tuple[Path, RenderPlan, Any]
    ) -> None:
        directory, _, _ = rendered
        index = read_index(directory)
        for clip in index.clips:
            base = directory.joinpath(clip.directory)
            assert base.joinpath(CLIP_FILENAME).stat().st_size == clip.size_bytes
            assert base.joinpath(SUBTITLES_ASS_FILENAME).stat().st_size == clip.ass_size_bytes

    def test_the_index_is_valid_json_with_lf_endings(
        self, rendered: tuple[Path, RenderPlan, Any]
    ) -> None:
        directory, _, _ = rendered
        raw = directory.joinpath(RENDER_INDEX_FILENAME).read_bytes()
        assert b"\r\n" not in raw
        assert json.loads(raw.decode("utf-8"))["clips"]

    def test_nothing_temporary_is_left_behind(self, rendered: tuple[Path, RenderPlan, Any]) -> None:
        directory, _, _ = rendered
        assert [path.name for path in directory.iterdir() if path.name.startswith(".")] == []
        assert list(directory.rglob("*.tmp")) == []

    def test_a_finished_set_verifies_and_is_reused_untouched(
        self, rendered: tuple[Path, RenderPlan, Any]
    ) -> None:
        directory, plan, outcome = rendered
        before = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

        verify_clips(directory, outcome.fingerprint, outcome.stage_config_sha256, plan)

        after = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        assert after == before


class TestRealFailures:
    def test_a_source_ffmpeg_cannot_read_fails_without_artifacts(self, tmp_path: Path) -> None:
        broken = tmp_path.joinpath("no es un contenedor.mp4")
        broken.write_bytes(b"this is not a container")
        directory = tmp_path.joinpath("clips")

        plan = plan_for(broken)

        with pytest.raises(RenderError):
            render(plan, directory)

        survivors = sorted(path.name for path in directory.rglob("*")) if directory.exists() else []
        assert survivors == []

    def test_a_source_without_audio_fails_rather_than_producing_a_silent_clip(
        self, awkward_root: Path, tmp_path: Path
    ) -> None:
        silent = awkward_root.joinpath("sin audio.mp4")
        if not silent.is_file():
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
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(silent),
                ],
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        directory = tmp_path.joinpath("clips")

        plan = plan_for(silent)

        with pytest.raises(RenderError):
            render(plan, directory)

        assert not directory.joinpath(RENDER_INDEX_FILENAME).exists()

    def test_a_failed_regeneration_leaves_the_previous_clips_verifiable(
        self, wide_source: Path, tmp_path: Path
    ) -> None:
        directory = tmp_path.joinpath("clips")
        plan = plan_for(wide_source)
        outcome = render(plan, directory)
        before = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        broken = tmp_path.joinpath("roto.mp4")
        broken.write_bytes(b"not a container")

        unreadable = plan_for(broken)

        with pytest.raises(RenderError):
            render(unreadable, directory)

        after = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        assert after == before
        assert verify_clips(directory, outcome.fingerprint, outcome.stage_config_sha256, plan).clips
