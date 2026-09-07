"""A set of clips has to describe the run in front of it, field by field.

The fingerprint proves the index and the stage configuration were written
together. It cannot prove they describe *this* run: a clips directory copied
from another experiment rebuilds its own fingerprint perfectly and hands
somebody a video of the wrong material under the right name.

Every branch here is one way that copy shows itself, and each is asserted
separately rather than through one "incoherent" case, because the whole point of
returning the first specific problem is that an operator is told which of them
happened.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from content_engine.config import load_settings
from content_engine.domain.enums import RenderPreset, ReviewDecisionType
from content_engine.domain.render_rules import render_coherence_problem, render_stage_config
from content_engine.domain.render_targets import RenderTarget, RenderTargetClip
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
    ClipRecord,
    RenderIndex,
    RenderStageConfig,
)
from tests.conftest import CANDIDATE_POLICY, chunk_of, collect, raw_candidate, speech_transcript

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def candidates(count: int = 2) -> list[Any]:
    transcript = speech_transcript()
    proposals = [raw_candidate(index * 30.0, index * 30.0 + 29.0) for index in range(count)]
    collection = collect(
        chunk_of(transcript), proposals, CANDIDATE_POLICY, transcript.duration_seconds
    )
    assert len(collection.candidates) == count
    return collection.candidates


def record(candidate: Any, **overrides: Any) -> ClipRecord:
    payload: dict[str, Any] = {
        "candidate_id": candidate.id,
        "rank": candidate.rank,
        "decision": ReviewDecisionType.APPROVED,
        "original_start": candidate.start,
        "original_end": candidate.end,
        "start": candidate.start,
        "end": candidate.end,
        "duration": candidate.duration,
        "directory": f"clip_{candidate.id}",
        "clip_filename": CLIP_FILENAME,
        "srt_filename": SUBTITLES_SRT_FILENAME,
        "ass_filename": SUBTITLES_ASS_FILENAME,
        "metadata_filename": CLIP_METADATA_FILENAME,
        "width": 1080,
        "height": 1920,
        "sample_aspect_ratio": "1:1",
        "video_codec": "h264",
        "audio_codec": "aac",
        "measured_duration_seconds": candidate.duration,
        "sha256": "a" * 64,
        "size_bytes": 4096,
        "srt_sha256": "b" * 64,
        "srt_size_bytes": 120,
        "ass_sha256": "c" * 64,
        "ass_size_bytes": 900,
        "cue_count": 4,
        "subtitles_burned": True,
    }
    payload.update(overrides)
    return ClipRecord(**payload)


def index_of(records: list[ClipRecord], **overrides: Any) -> RenderIndex:
    payload: dict[str, Any] = {
        "generated_at": NOW,
        "analysis_fingerprint": "1" * 64,
        "review_fingerprint": "2" * 64,
        "decisions_sha256": "3" * 64,
        "transcript_sha256": "4" * 64,
        "source_sha256": "5" * 64,
        "source_duration_seconds": 500.0,
        "width": 1080,
        "height": 1920,
        "preset": RenderPreset.VERTICAL_BLUR,
        "burn_subtitles": True,
        "clips": records,
    }
    payload.update(overrides)
    return RenderIndex(**payload)


def target_of(kept: list[Any]) -> RenderTarget:
    return RenderTarget(
        clips=tuple(
            RenderTargetClip(
                candidate=candidate,
                decision=ReviewDecisionType.APPROVED,
                start=candidate.start,
                end=candidate.end,
            )
            for candidate in kept
        ),
        analysis_fingerprint="1" * 64,
        review_fingerprint="2" * 64,
        decisions_sha256="3" * 64,
        transcript_sha256="4" * 64,
        source_sha256="5" * 64,
        source_duration_seconds=500.0,
    )


def config_of(**overrides: Any) -> RenderStageConfig:
    built = render_stage_config(load_settings().render)
    if not overrides:
        return built
    return RenderStageConfig.model_validate(built.model_dump(mode="json") | overrides)


class TestCoherent:
    def test_a_matching_set_reports_no_problem(self) -> None:
        kept = candidates()
        index = index_of([record(candidate) for candidate in kept])

        assert render_coherence_problem(index, config_of(), target_of(kept)) is None


class TestIdentity:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("analysis_fingerprint", "analysis"),
            ("review_fingerprint", "review"),
            ("decisions_sha256", "decision file"),
            ("transcript_sha256", "transcript"),
            ("source_sha256", "source"),
        ],
    )
    def test_a_set_built_from_other_material_is_named(self, field: str, expected: str) -> None:
        kept = candidates()
        index = index_of([record(candidate) for candidate in kept], **{field: "9" * 64})

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert expected in problem


class TestPolicy:
    def test_other_dimensions_are_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0], width=720, height=1280)], width=720, height=1280)

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "720x1280" in problem

    def test_another_preset_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0])], preset=RenderPreset.VERTICAL_CROP)

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "preset" in problem

    def test_a_different_burn_setting_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0], subtitles_burned=False)], burn_subtitles=False)

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "burn_subtitles" in problem

    def test_another_rules_version_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0])], rules_version=99)

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "render rules" in problem

    def test_another_candidate_schema_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0])])

        problem = render_coherence_problem(
            index, config_of(candidates_schema_version=99), target_of(kept)
        )

        assert problem is not None
        assert "candidate schema" in problem

    def test_another_decision_schema_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0])])

        problem = render_coherence_problem(
            index, config_of(decisions_schema_version=99), target_of(kept)
        )

        assert problem is not None
        assert "decision schema" in problem


class TestShortlist:
    def test_a_kept_candidate_with_no_clip_is_named(self) -> None:
        kept = candidates(2)
        index = index_of([record(kept[0])])

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "has no clip" in problem

    def test_a_clip_recording_another_decision_is_named(self) -> None:
        kept = candidates(1)
        index = index_of(
            [
                record(
                    kept[0],
                    decision=ReviewDecisionType.EDITED,
                    start=kept[0].start + 1.0,
                    duration=kept[0].duration - 1.0,
                    measured_duration_seconds=kept[0].duration - 1.0,
                )
            ]
        )

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "decision" in problem

    def test_a_clip_over_another_interval_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0])])
        target = target_of(kept)
        moved = replace(
            target,
            clips=(replace(target.clips[0], start=kept[0].start + 4.0),),
        )

        problem = render_coherence_problem(index, config_of(), moved)

        assert problem is not None
        assert "covers" in problem

    def test_a_clip_of_another_rank_is_named(self) -> None:
        kept = candidates(1)
        index = index_of([record(kept[0], rank=7)])

        problem = render_coherence_problem(index, config_of(), target_of(kept))

        assert problem is not None
        assert "ranked" in problem

    def test_a_clip_for_a_candidate_the_review_did_not_keep_is_named(self) -> None:
        """The refusal that stops a rejection from being published anyway."""
        kept = candidates(2)
        index = index_of([record(candidate) for candidate in kept])

        problem = render_coherence_problem(index, config_of(), target_of(kept[:1]))

        assert problem is not None
        assert "did not keep" in problem
        assert "rejected candidate must not have one" in problem
