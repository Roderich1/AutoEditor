"""CE-034: the pure half of preview generation.

Everything asserted here is a decision that must be reproducible from the
arguments alone: which command runs, what it is asked to produce, and how a
finished set of previews is identified. No file is written and no encoder runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from content_engine.domain.preview_rules import (
    PREVIEW_ARGUMENT_VERSION,
    PREVIEW_DURATION_TOLERANCE_SECONDS,
    PREVIEW_RULES_VERSION,
    preview_arguments,
    preview_coherence_problem,
    preview_filename,
    preview_fingerprint,
    preview_stage_config,
    preview_stage_config_sha256,
    preview_video_filter,
)
from content_engine.domain.previews import (
    PREVIEW_INDEX_SCHEMA_VERSION,
    PreviewIndex,
    PreviewRecord,
    PreviewStageConfig,
)
from pydantic import ValidationError

from content_engine.config import Settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.run_state import (
    ALLOWED_TRANSITIONS,
    failure_status,
    is_allowed,
    stage_for_failure,
    success_status,
)
from tests.conftest import chunk_of, collect, raw_candidate, speech_transcript

FINGERPRINT = "a" * 64
SOURCE_SHA = "c" * 64
GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def config(width: int = 540, height: int = 960) -> PreviewStageConfig:
    return preview_stage_config(width=width, height=height)


def record(candidate_id: str = "cand_0001", rank: int = 1, **overrides: Any) -> PreviewRecord:
    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "rank": rank,
        "start": 10.0,
        "end": 39.0,
        "duration": 29.0,
        "filename": preview_filename(candidate_id),
        "width": 540,
        "height": 960,
        "measured_duration_seconds": 29.02,
        "video_codec": "h264",
        "audio_codec": "aac",
        "sha256": "d" * 64,
        "size_bytes": 4096,
    }
    payload.update(overrides)
    return PreviewRecord(**payload)


def index(*records: PreviewRecord, **overrides: Any) -> PreviewIndex:
    payload: dict[str, Any] = {
        "generated_at": GENERATED_AT,
        "analysis_fingerprint": FINGERPRINT,
        "source_sha256": SOURCE_SHA,
        "source_duration_seconds": 119.0,
        "width": 540,
        "height": 960,
        "previews": list(records),
    }
    payload.update(overrides)
    return PreviewIndex(**payload)


class TestStageIntegration:
    """Preview and review are stages of their own, not part of analysis."""

    def test_preview_produces_ready_for_review(self) -> None:
        assert success_status(RunStage.PREVIEW) is RunStatus.READY_FOR_REVIEW
        assert failure_status(RunStage.PREVIEW) is RunStatus.FAILED_PREVIEW

    def test_review_produces_reviewed(self) -> None:
        assert success_status(RunStage.REVIEW) is RunStatus.REVIEWED
        assert failure_status(RunStage.REVIEW) is RunStatus.FAILED_REVIEW

    def test_each_new_failure_state_names_its_stage(self) -> None:
        assert stage_for_failure(RunStatus.FAILED_PREVIEW) is RunStage.PREVIEW
        assert stage_for_failure(RunStatus.FAILED_REVIEW) is RunStage.REVIEW

    def test_a_preview_failure_is_reachable_from_analyzed(self) -> None:
        assert is_allowed(RunStatus.ANALYZED, RunStatus.FAILED_PREVIEW)

    def test_a_preview_failure_is_not_reachable_before_analysis(self) -> None:
        assert RunStatus.FAILED_PREVIEW not in ALLOWED_TRANSITIONS[RunStatus.TRANSCRIBED]

    def test_a_failing_encoder_never_produces_ready_for_review(self) -> None:
        assert not is_allowed(RunStatus.FAILED_PREVIEW, RunStatus.REVIEWED)

    def test_a_failed_preview_can_be_retried(self) -> None:
        assert is_allowed(RunStatus.FAILED_PREVIEW, RunStatus.READY_FOR_REVIEW)

    def test_review_can_be_reopened_explicitly(self) -> None:
        """ADR-030. The only backwards edge in the machine, and it is deliberate."""
        assert is_allowed(RunStatus.REVIEWED, RunStatus.READY_FOR_REVIEW)

    def test_reopening_does_not_reach_further_back(self) -> None:
        assert not is_allowed(RunStatus.REVIEWED, RunStatus.ANALYZED)
        assert not is_allowed(RunStatus.READY_FOR_REVIEW, RunStatus.ANALYZED)


class TestFilenames:
    def test_the_filename_names_the_candidate(self) -> None:
        assert preview_filename("cand_a9652f6a") == "candidate_cand_a9652f6a.mp4"

    @pytest.mark.parametrize(
        "candidate_id",
        ["../escape", "a/b", "a\\b", "", "cand id", "cand:1", "."],
    )
    def test_an_unsafe_identifier_is_refused(self, candidate_id: str) -> None:
        with pytest.raises(ValueError, match="identifier"):
            preview_filename(candidate_id)


class TestVideoFilter:
    def test_the_frame_is_fitted_and_padded_rather_than_stretched(self) -> None:
        assert preview_video_filter(config()) == (
            "scale=540:960:force_original_aspect_ratio=decrease,"
            "pad=540:960:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )

    def test_the_filter_follows_the_configured_dimensions(self) -> None:
        assert "scale=360:640" in preview_video_filter(config(width=360, height=640))


class TestArguments:
    def build(self, **kwargs: Any) -> list[str]:
        defaults: dict[str, Any] = {
            "source": Path("/videos/clase.mp4"),
            "start": 10.0,
            "duration": 29.0,
            "output": Path("/runs/x/previews/candidate_cand_1.mp4"),
            "config": config(),
        }
        defaults.update(kwargs)
        return preview_arguments(**defaults)

    def test_every_argument_is_a_separate_string(self) -> None:
        arguments = self.build()
        assert all(isinstance(argument, str) for argument in arguments)

    def test_the_command_is_ffmpeg(self) -> None:
        assert self.build()[0] == "ffmpeg"

    def test_no_argument_is_a_shell_construct(self) -> None:
        """FFmpeg is never handed a string a shell could reinterpret."""
        for argument in self.build():
            assert not any(token in argument for token in ("&&", "||", ";", "|", ">", "<", "$("))

    def test_stdin_is_closed_and_output_overwritten(self) -> None:
        arguments = self.build()
        assert "-nostdin" in arguments
        assert "-y" in arguments

    def test_the_window_is_sought_before_the_input_and_limited_after_it(self) -> None:
        arguments = self.build()
        assert arguments[arguments.index("-ss") + 1] == "10.000"
        assert arguments.index("-ss") < arguments.index("-i")
        assert arguments[arguments.index("-t") + 1] == "29.000"
        assert arguments.index("-i") < arguments.index("-t")

    def test_sub_second_bounds_survive_as_milliseconds(self) -> None:
        arguments = self.build(start=1285.02, duration=63.34)
        assert arguments[arguments.index("-ss") + 1] == "1285.020"
        assert arguments[arguments.index("-t") + 1] == "63.340"

    def test_the_source_and_the_output_are_the_only_paths(self) -> None:
        source = Path("/videos/mi vídeo ñandú.mp4")
        output = Path("/runs/ñ/previews/candidate_cand_1.mp4")
        arguments = self.build(source=source, output=output)
        assert arguments[arguments.index("-i") + 1] == str(source)
        assert arguments[-1] == str(output)

    def test_one_video_and_one_audio_stream_are_mapped(self) -> None:
        arguments = self.build()
        assert arguments[arguments.index("-map") + 1] == "0:v:0"
        assert "0:a:0" in arguments

    def test_subtitle_and_data_streams_are_dropped(self) -> None:
        arguments = self.build()
        assert "-sn" in arguments
        assert "-dn" in arguments

    def test_no_subtitle_filter_is_applied(self) -> None:
        """CE-040 to CE-042 own subtitles. A preview must carry none."""
        assert "subtitles" not in " ".join(self.build())

    def test_the_encode_is_the_cheap_one(self) -> None:
        arguments = self.build()
        assert arguments[arguments.index("-c:v") + 1] == "libx264"
        assert arguments[arguments.index("-preset") + 1] == "veryfast"
        assert arguments[arguments.index("-crf") + 1] == "30"
        assert arguments[arguments.index("-pix_fmt") + 1] == "yuv420p"

    def test_audio_is_kept_as_aac(self) -> None:
        arguments = self.build()
        assert arguments[arguments.index("-c:a") + 1] == "aac"
        assert arguments[arguments.index("-b:a") + 1] == "96k"

    def test_the_filter_is_passed_once(self) -> None:
        arguments = self.build()
        assert arguments.count("-vf") == 1
        assert arguments[arguments.index("-vf") + 1] == preview_video_filter(config())


class TestStageConfig:
    def test_it_is_built_from_the_preview_settings(self, settings: Settings) -> None:
        built = preview_stage_config(width=settings.preview.width, height=settings.preview.height)
        assert (built.width, built.height) == (540, 960)

    def test_it_records_the_rule_and_argument_versions(self) -> None:
        built = config()
        assert built.preview_rules_version == PREVIEW_RULES_VERSION
        assert built.argument_version == PREVIEW_ARGUMENT_VERSION
        assert built.index_schema_version == PREVIEW_INDEX_SCHEMA_VERSION

    def test_it_records_the_tolerance_it_verifies_against(self) -> None:
        assert config().duration_tolerance_seconds == PREVIEW_DURATION_TOLERANCE_SECONDS

    def test_the_digest_is_a_sha256(self) -> None:
        assert len(preview_stage_config_sha256(config())) == 64

    def test_it_carries_no_path_from_the_machine(self) -> None:
        values = "".join(str(value) for value in config().model_dump(mode="json").values())
        assert "/" not in values
        assert "\\" not in values

    def test_odd_dimensions_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            preview_stage_config(width=541, height=960)

    def test_the_digest_changes_with_the_dimensions(self) -> None:
        assert preview_stage_config_sha256(config()) != preview_stage_config_sha256(
            config(width=360, height=640)
        )


class TestIndexInvariants:
    def test_a_matching_index_is_valid(self) -> None:
        assert index(record()).previews[0].candidate_id == "cand_0001"

    def test_an_empty_index_is_valid(self) -> None:
        """CE-034 with nothing selected: an honest record of zero previews."""
        assert index().previews == []

    def test_a_declared_duration_must_match_the_interval(self) -> None:
        with pytest.raises(ValidationError, match="duration"):
            index(record(duration=30.0))

    def test_a_filename_that_names_another_candidate_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="filename"):
            index(record(filename="candidate_cand_other.mp4"))

    def test_a_filename_with_a_path_separator_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            index(record(filename="../candidate_cand_0001.mp4"))

    def test_dimensions_must_match_the_index(self) -> None:
        with pytest.raises(ValidationError, match="540"):
            index(record(width=360))

    def test_a_repeated_candidate_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="more than once"):
            index(record(), record())

    def test_ranks_must_be_contiguous_from_one(self) -> None:
        with pytest.raises(ValidationError, match="rank"):
            index(record("cand_0001", rank=1), record("cand_0002", rank=3))

    def test_a_preview_reaching_past_the_source_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="source"):
            index(record(start=100.0, end=130.0, duration=30.0, measured_duration_seconds=30.0))

    def test_a_measured_duration_far_from_the_interval_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="measured"):
            index(record(measured_duration_seconds=45.0))

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            index(record(size_bytes=0))


class TestFingerprint:
    def test_it_is_stable_for_identical_inputs(self) -> None:
        first = preview_fingerprint(FINGERPRINT, index(record()), config())
        second = preview_fingerprint(FINGERPRINT, index(record()), config())
        assert first == second

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda: preview_fingerprint("z" * 64, index(record()), config()),
            lambda: preview_fingerprint(FINGERPRINT, index(record(sha256="e" * 64)), config()),
            lambda: preview_fingerprint(FINGERPRINT, index(), config()),
            lambda: preview_fingerprint(
                FINGERPRINT, index(record()), config(width=360, height=640)
            ),
        ],
    )
    def test_any_change_moves_it(self, mutate: Any) -> None:
        assert mutate() != preview_fingerprint(FINGERPRINT, index(record()), config())


class TestCoherence:
    def shortlist(self) -> list[Any]:
        collection = collect(
            chunk_of(speech_transcript()),
            [raw_candidate(10.0, 39.0), raw_candidate(60.0, 89.0, hook=88)],
        )
        return list(collection.candidates)

    def matching_index(self) -> PreviewIndex:
        records = [
            record(
                candidate.id,
                rank=position,
                start=candidate.start,
                end=candidate.end,
                duration=candidate.duration,
                filename=preview_filename(candidate.id),
                measured_duration_seconds=candidate.duration,
            )
            for position, candidate in enumerate(self.shortlist(), start=1)
        ]
        return index(*records)

    def test_a_matching_set_has_no_problem(self) -> None:
        assert (
            preview_coherence_problem(
                self.matching_index(), config(), FINGERPRINT, SOURCE_SHA, self.shortlist()
            )
            is None
        )

    def test_another_analysis_is_refused(self) -> None:
        problem = preview_coherence_problem(
            self.matching_index(), config(), "z" * 64, SOURCE_SHA, self.shortlist()
        )
        assert problem is not None
        assert "analysis" in problem

    def test_another_source_file_is_refused(self) -> None:
        problem = preview_coherence_problem(
            self.matching_index(), config(), FINGERPRINT, "z" * 64, self.shortlist()
        )
        assert problem is not None
        assert "source" in problem

    def test_a_missing_candidate_is_refused(self) -> None:
        problem = preview_coherence_problem(
            index(), config(), FINGERPRINT, SOURCE_SHA, self.shortlist()
        )
        assert problem is not None
        assert "candidate" in problem

    def test_a_moved_interval_is_refused(self) -> None:
        candidates = self.shortlist()
        records = list(self.matching_index().previews)
        shifted = record(
            candidates[0].id,
            rank=1,
            start=candidates[0].start + 1.0,
            end=candidates[0].end + 1.0,
            duration=candidates[0].duration,
            measured_duration_seconds=candidates[0].duration,
        )
        moved = index(shifted, *records[1:])
        problem = preview_coherence_problem(moved, config(), FINGERPRINT, SOURCE_SHA, candidates)
        assert problem is not None
        assert "interval" in problem

    def test_different_dimensions_are_refused(self) -> None:
        problem = preview_coherence_problem(
            self.matching_index(),
            config(width=360, height=640),
            FINGERPRINT,
            SOURCE_SHA,
            self.shortlist(),
        )
        assert problem is not None
        assert "dimension" in problem
