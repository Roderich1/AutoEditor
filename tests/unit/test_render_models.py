"""The render stage's artifacts refuse to describe something that did not happen.

Every model here is read back off disk by a later invocation and believed, so
each one checks the claims it can check against itself: that a record's declared
duration is its interval, that its directory names its candidate, that a clip
recorded as approved kept the boundaries it was approved with, and that an index
does not hold two records for one candidate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from content_engine.domain.enums import RenderPreset, ReviewDecisionType
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    RENDER_DURATION_TOLERANCE_SECONDS,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
    ClipRecord,
    RenderIndex,
    clip_dirname,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def record(**overrides: Any) -> ClipRecord:
    payload: dict[str, Any] = {
        "candidate_id": "cand_0001",
        "rank": 1,
        "decision": ReviewDecisionType.APPROVED,
        "original_start": 10.0,
        "original_end": 40.0,
        "start": 10.0,
        "end": 40.0,
        "duration": 30.0,
        "directory": "clip_cand_0001",
        "clip_filename": CLIP_FILENAME,
        "srt_filename": SUBTITLES_SRT_FILENAME,
        "ass_filename": SUBTITLES_ASS_FILENAME,
        "metadata_filename": CLIP_METADATA_FILENAME,
        "width": 1080,
        "height": 1920,
        "sample_aspect_ratio": "1:1",
        "video_codec": "h264",
        "audio_codec": "aac",
        "measured_duration_seconds": 30.02,
        "sha256": DIGEST,
        "size_bytes": 4096,
        "srt_sha256": OTHER_DIGEST,
        "srt_size_bytes": 120,
        "ass_sha256": "c" * 64,
        "ass_size_bytes": 900,
        "cue_count": 12,
        "subtitles_burned": True,
    }
    payload.update(overrides)
    return ClipRecord(**payload)


def index(**overrides: Any) -> RenderIndex:
    payload: dict[str, Any] = {
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analysis_fingerprint": DIGEST,
        "review_fingerprint": OTHER_DIGEST,
        "decisions_sha256": "d" * 64,
        "transcript_sha256": "e" * 64,
        "source_sha256": "f" * 64,
        "source_duration_seconds": 2000.0,
        "width": 1080,
        "height": 1920,
        "preset": RenderPreset.VERTICAL_BLUR,
        "burn_subtitles": True,
        "clips": [record()],
    }
    payload.update(overrides)
    return RenderIndex(**payload)


class TestClipDirname:
    def test_it_names_the_candidate(self) -> None:
        assert clip_dirname("cand_abc123") == "clip_cand_abc123"

    @pytest.mark.parametrize(
        "identifier", ["", "../escape", "a/b", "a\\b", "a:b", ".", "with space"]
    )
    def test_an_unsafe_identifier_is_refused_rather_than_sanitised(self, identifier: str) -> None:
        with pytest.raises(ValueError, match="not safe"):
            clip_dirname(identifier)


class TestClipRecord:
    def test_a_well_formed_record_is_accepted(self) -> None:
        assert record().candidate_id == "cand_0001"

    def test_an_inverted_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at or before its start"):
            record(start=40.0, end=10.0, duration=30.0)

    def test_an_inverted_original_interval_is_refused(self) -> None:
        """The bounds a person was shown, which a decision copied from the candidate."""
        with pytest.raises(ValueError, match="original interval"):
            record(original_start=40.0, original_end=10.0, decision=ReviewDecisionType.EDITED)

    def test_a_declared_duration_that_is_not_the_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="declares duration"):
            record(duration=25.0)

    def test_a_directory_that_names_another_candidate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not name candidate"):
            record(directory="clip_cand_9999")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("clip_filename", "video.mp4"),
            ("srt_filename", "subs.srt"),
            ("ass_filename", "subs.ass"),
            ("metadata_filename", "meta.json"),
        ],
    )
    def test_a_renamed_artifact_is_refused(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match="expected"):
            record(**{field: value})

    def test_a_measured_duration_beyond_the_tolerance_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tolerance"):
            record(measured_duration_seconds=30.0 + RENDER_DURATION_TOLERANCE_SECONDS + 0.5)

    def test_a_measured_duration_inside_the_tolerance_is_accepted(self) -> None:
        assert record(measured_duration_seconds=30.0 + RENDER_DURATION_TOLERANCE_SECONDS).duration

    def test_an_approved_clip_may_not_have_moved_its_boundaries(self) -> None:
        with pytest.raises(ValueError, match="approved"):
            record(start=11.0, duration=29.0)

    def test_an_edited_clip_must_have_moved_them(self) -> None:
        with pytest.raises(ValueError, match="edited"):
            record(decision=ReviewDecisionType.EDITED)

    def test_an_edited_clip_with_moved_boundaries_is_accepted(self) -> None:
        edited = record(
            decision=ReviewDecisionType.EDITED,
            start=12.0,
            duration=28.0,
            measured_duration_seconds=28.02,
        )

        assert edited.start == 12.0

    def test_a_rejected_decision_can_never_become_a_clip(self) -> None:
        with pytest.raises(ValueError):
            record(decision=ReviewDecisionType.REJECTED)

    def test_a_clip_with_no_audio_is_refused(self) -> None:
        with pytest.raises(ValueError, match="audio"):
            record(audio_codec=None)

    def test_extra_fields_are_refused(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            record(unexpected=1)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_number_is_refused(self, value: float) -> None:
        with pytest.raises(ValueError):
            record(measured_duration_seconds=value)

    def test_a_negative_cue_count_is_refused(self) -> None:
        with pytest.raises(ValueError):
            record(cue_count=-1)

    def test_a_clip_with_no_cues_is_accepted(self) -> None:
        """Silence is renderable; an empty subtitle file is the honest record of it."""
        assert record(cue_count=0).cue_count == 0


class TestRenderIndex:
    def test_a_well_formed_index_is_accepted(self) -> None:
        assert len(index().clips) == 1

    def test_two_records_for_one_candidate_are_refused(self) -> None:
        twice = [record(), record()]

        with pytest.raises(ValueError, match="more than once"):
            index(clips=twice)

    def test_ranks_may_have_gaps_because_rejections_leave_them(self) -> None:
        built = index(
            clips=[
                record(),
                record(candidate_id="cand_0002", rank=4, directory="clip_cand_0002"),
            ]
        )

        assert [clip.rank for clip in built.clips] == [1, 4]

    def test_ranks_out_of_order_are_refused(self) -> None:
        backwards = [
            record(candidate_id="cand_0002", rank=4, directory="clip_cand_0002"),
            record(),
        ]

        with pytest.raises(ValueError, match="rank order"):
            index(clips=backwards)

    def test_a_repeated_rank_is_refused(self) -> None:
        repeated = [
            record(),
            record(candidate_id="cand_0002", rank=1, directory="clip_cand_0002"),
        ]

        with pytest.raises(ValueError, match="rank order"):
            index(clips=repeated)

    def test_a_clip_of_other_dimensions_is_refused(self) -> None:
        mismatched = [record(width=720, height=1280)]

        with pytest.raises(ValueError, match="in an index of"):
            index(clips=mismatched)

    def test_a_clip_disagreeing_about_the_burn_setting_is_refused(self) -> None:
        unburned = [record(subtitles_burned=False)]

        with pytest.raises(ValueError, match="subtitles_burned"):
            index(clips=unburned)

    def test_a_clip_reaching_past_the_source_is_refused(self) -> None:
        with pytest.raises(ValueError, match="beyond the source"):
            index(source_duration_seconds=20.0)

    def test_an_empty_index_is_accepted(self) -> None:
        """A review that rejected everything is a result, not a failure."""
        assert index(clips=[]).clips == []

    def test_by_candidate_maps_every_record(self) -> None:
        assert set(index().by_candidate) == {"cand_0001"}

    def test_extra_fields_are_refused(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            index(unexpected=1)
