"""The refusals that keep a preview set from being trusted on appearances.

Every check in this file guards a path that only opens when something has
already gone wrong: an encoder that reports success and writes nothing, an
artifact edited between two runs, a manifest that names a schema this build does
not produce. They are the branches that decide whether a bad state is caught or
inherited, so they are exercised deliberately rather than left to chance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.media.preview import FFmpegPreviewRenderer
from content_engine.domain.candidates import (
    BoundaryAdjustment,
    CandidateCollection,
    ValidatedCandidate,
)
from content_engine.domain.enums import BoundaryAnchor, CandidateStatus, ClipCategory, RunStage
from content_engine.domain.exceptions import (
    EXIT_INVALID_INPUT,
    EXIT_SUCCESS,
    IncompatibleArtifactError,
    RenderError,
)
from content_engine.domain.models import MediaInfo
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    PREVIEW_STAGE_CONFIG_FILENAME,
    preview_coherence_problem,
    preview_filename,
    preview_stage_config,
)
from content_engine.domain.previews import PreviewIndex, PreviewRecord, PreviewStageConfig
from content_engine.domain.review import (
    ApprovedDecision,
    ReviewDecisionCollection,
)
from content_engine.services.preview_service import (
    PreviewPlan,
    PreviewService,
    read_index,
    read_stage_config,
)
from content_engine.services.review_service import ReviewPlan, ReviewSession
from content_engine.utils.json import write_json
from tests.conftest import (
    Analysed,
    FakeMedia,
    chunk_of,
    cli_output,
    collect,
    raw_candidate,
    scores,
    speech_transcript,
)

runner = CliRunner()
FINGERPRINT = "a" * 64
SOURCE_SHA = "c" * 64
GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("source.mp4")
    path.write_bytes(b"source-video")
    return path


def shortlist() -> CandidateCollection:
    return collect(chunk_of(speech_transcript()), [raw_candidate(10.0, 39.0)])


def plan_for(source: Path, **overrides: Any) -> PreviewPlan:
    selection = shortlist()
    payload: dict[str, Any] = {
        "candidates": tuple(selection.candidates),
        "config": preview_stage_config(width=540, height=960),
        "analysis_fingerprint": FINGERPRINT,
        "source_path": source,
        "source_sha256": SOURCE_SHA,
        "source_duration_seconds": selection.source_duration_seconds,
    }
    payload.update(overrides)
    return PreviewPlan(**payload)


class FakeProbe:
    """Answers about a preview without an encoder, so a defect can be placed."""

    def __init__(self, **overrides: Any) -> None:
        self.overrides = overrides

    def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, Any]]:
        payload: dict[str, Any] = {
            "duration_seconds": 29.0,
            "video_codec": "h264",
            "width": 540,
            "height": 960,
            "fps": 30.0,
            "audio_codec": "aac",
            "sample_rate": 44100,
            "channels": 2,
            "container": "mov,mp4,m4a",
            "file_size": 1024,
        }
        payload.update(self.overrides)
        return MediaInfo(**payload), {}


class WritingRenderer:
    """A renderer that always succeeds, so the checks after it can be reached."""

    def render(
        self, source: Path, start: float, duration: float, output: Path, config: Any
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"placeholder preview")


class TestTheEncoderIsNotTakenAtItsWord:
    def test_a_silent_success_that_writes_nothing_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "content_engine.adapters.media.preview.run_command",
            lambda arguments, **_: None,
        )
        with pytest.raises(RenderError, match="produced no preview"):
            FFmpegPreviewRenderer().render(
                tmp_path.joinpath("in.mp4"),
                0.0,
                1.0,
                tmp_path.joinpath("out.mp4"),
                preview_stage_config(width=540, height=960),
            )

    def test_an_empty_file_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path.joinpath("out.mp4")

        def touch(arguments: Any, **_: Any) -> None:
            output.write_bytes(b"")

        monkeypatch.setattr("content_engine.adapters.media.preview.run_command", touch)
        with pytest.raises(RenderError, match="produced no preview"):
            FFmpegPreviewRenderer().render(
                tmp_path.joinpath("in.mp4"),
                0.0,
                1.0,
                output,
                preview_stage_config(width=540, height=960),
            )

    def test_a_preview_in_the_wrong_codec_is_refused(self, tmp_path: Path, source: Path) -> None:
        service = PreviewService(WritingRenderer(), FakeProbe(video_codec="vp9"))
        with pytest.raises(RenderError, match="vp9"):
            service.generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)

    def test_a_preview_reported_without_audio_is_refused(
        self, tmp_path: Path, source: Path
    ) -> None:
        service = PreviewService(WritingRenderer(), FakeProbe(audio_codec=None))
        with pytest.raises(RenderError, match="no audio stream"):
            service.generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)

    def test_an_unselected_candidate_is_refused(self, tmp_path: Path, source: Path) -> None:
        """A candidate with no rank was never selected, so nobody will be shown it."""
        unranked = ValidatedCandidate(
            id="cand_unranked",
            chunk_id="chunk_0000",
            rank=None,
            start=10.0,
            end=39.0,
            duration=29.0,
            category=ClipCategory.EXPLANATION,
            topic="tema",
            hook="gancho",
            summary="resumen",
            reason="motivo",
            scores=scores(),
            total_score=90.0,
            score_formula_version=1,
            boundary=BoundaryAdjustment(
                proposed_start=10.0,
                proposed_end=39.0,
                adjusted_start=10.0,
                adjusted_end=39.0,
                start_delta=0.0,
                end_delta=0.0,
                start_anchor=BoundaryAnchor.UNCHANGED,
                end_anchor=BoundaryAnchor.UNCHANGED,
                window_seconds=2.5,
                reverted=False,
            ),
            status=CandidateStatus.SUGGESTED,
        )
        service = PreviewService(WritingRenderer(), FakeProbe())
        with pytest.raises(RenderError, match="no rank"):
            service.generate(
                plan_for(source, candidates=(unranked,)),
                tmp_path.joinpath("previews"),
                GENERATED_AT,
            )

    def test_an_index_the_stage_cannot_describe_is_refused(
        self, tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last line of defence: records that pass their own schema and
        still do not describe the run they were built for."""
        monkeypatch.setattr(
            "content_engine.services.preview_service.preview_coherence_problem",
            lambda *_, **__: "a synthetic disagreement",
        )
        service = PreviewService(WritingRenderer(), FakeProbe())
        with pytest.raises(RenderError, match="synthetic disagreement"):
            service.generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)

    def test_an_index_that_fails_its_own_schema_is_refused_as_a_render_error(
        self, tmp_path: Path, source: Path
    ) -> None:
        """A pydantic error must not reach the CLI as an unexpected fault."""
        selection = collect(
            chunk_of(speech_transcript()),
            [raw_candidate(10.0, 39.0), raw_candidate(60.0, 89.0, hook=88)],
        )
        # Only the second-ranked candidate, so the index ranks start at 2.
        service = PreviewService(WritingRenderer(), FakeProbe())
        with pytest.raises(RenderError, match="cannot describe"):
            service.generate(
                plan_for(source, candidates=(selection.candidates[1],)),
                tmp_path.joinpath("previews"),
                GENERATED_AT,
            )


class TestRecordInvariants:
    def test_an_inverted_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="before its start"):
            PreviewRecord(
                candidate_id="cand_0001",
                rank=1,
                start=39.0,
                end=10.0,
                duration=0.5,
                filename=preview_filename("cand_0001"),
                width=540,
                height=960,
                measured_duration_seconds=0.5,
                video_codec="h264",
                audio_codec="aac",
                sha256="d" * 64,
                size_bytes=10,
            )


class TestCoherenceEdges:
    def config(self, **overrides: Any) -> PreviewStageConfig:
        base = preview_stage_config(width=540, height=960).model_dump()
        base.update(overrides)
        return PreviewStageConfig(**base)

    def record(self, candidate: ValidatedCandidate, rank: int) -> PreviewRecord:
        return PreviewRecord(
            candidate_id=candidate.id,
            rank=rank,
            start=candidate.start,
            end=candidate.end,
            duration=candidate.duration,
            filename=preview_filename(candidate.id),
            width=540,
            height=960,
            measured_duration_seconds=candidate.duration,
            video_codec="h264",
            audio_codec="aac",
            sha256="d" * 64,
            size_bytes=10,
        )

    def index(self, candidates: list[ValidatedCandidate], **overrides: Any) -> PreviewIndex:
        payload: dict[str, Any] = {
            "generated_at": GENERATED_AT,
            "analysis_fingerprint": FINGERPRINT,
            "source_sha256": SOURCE_SHA,
            "source_duration_seconds": 119.0,
            "width": 540,
            "height": 960,
            "previews": [
                self.record(candidate, position)
                for position, candidate in enumerate(candidates, start=1)
            ],
        }
        payload.update(overrides)
        return PreviewIndex(**payload)

    def test_an_index_cut_under_other_rules_is_refused(self) -> None:
        candidates = list(shortlist().candidates)
        problem = preview_coherence_problem(
            self.index(candidates, rules_version=99),
            self.config(),
            FINGERPRINT,
            SOURCE_SHA,
            candidates,
        )
        assert problem is not None
        assert "preview rules" in problem

    def test_previews_cut_from_another_candidate_schema_are_refused(self) -> None:
        candidates = list(shortlist().candidates)
        problem = preview_coherence_problem(
            self.index(candidates),
            self.config(candidates_schema_version=99),
            FINGERPRINT,
            SOURCE_SHA,
            candidates,
        )
        assert problem is not None
        assert "candidate schema" in problem

    def test_a_preview_filed_under_the_wrong_rank_is_refused(self) -> None:
        candidates = list(shortlist().candidates)
        moved = self.index(candidates).model_copy(
            update={"previews": [self.record(candidates[0], 2)]}
        )
        problem = preview_coherence_problem(
            moved, self.config(), FINGERPRINT, SOURCE_SHA, candidates
        )
        assert problem is not None
        assert "ranked" in problem


class TestArtifactsReadBack:
    @pytest.fixture
    def previews(self, tmp_path: Path, source: Path) -> Path:
        directory = tmp_path.joinpath("previews")
        PreviewService(WritingRenderer(), FakeProbe()).generate(
            plan_for(source), directory, GENERATED_AT
        )
        return directory

    def test_a_missing_index_is_refused(self, previews: Path) -> None:
        previews.joinpath(PREVIEW_INDEX_FILENAME).unlink()
        with pytest.raises(IncompatibleArtifactError, match="is missing"):
            read_index(previews)

    def test_an_unreadable_index_is_refused(self, previews: Path) -> None:
        previews.joinpath(PREVIEW_INDEX_FILENAME).write_text("{truncated", encoding="utf-8")
        with pytest.raises(IncompatibleArtifactError, match="cannot be read"):
            read_index(previews)

    def test_an_index_that_is_not_an_object_is_refused(self, previews: Path) -> None:
        write_json(previews.joinpath(PREVIEW_INDEX_FILENAME), [1, 2, 3])
        with pytest.raises(IncompatibleArtifactError, match="does not contain"):
            read_index(previews)

    def test_an_index_of_an_unknown_schema_is_refused(self, previews: Path) -> None:
        path = previews.joinpath(PREVIEW_INDEX_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        write_json(path, payload)
        with pytest.raises(IncompatibleArtifactError, match="index schema"):
            read_index(previews)

    def test_an_index_that_fails_validation_is_refused(self, previews: Path) -> None:
        path = previews.joinpath(PREVIEW_INDEX_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["previews"][0]["rank"] = 7
        write_json(path, payload)
        with pytest.raises(IncompatibleArtifactError, match="not a valid preview index"):
            read_index(previews)

    def test_a_stage_configuration_of_an_unknown_schema_is_refused(self, previews: Path) -> None:
        path = previews.joinpath(PREVIEW_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = 99
        write_json(path, payload)
        with pytest.raises(IncompatibleArtifactError, match="configuration schema"):
            read_stage_config(previews)

    def test_a_stage_configuration_that_fails_validation_is_refused(self, previews: Path) -> None:
        path = previews.joinpath(PREVIEW_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["crf"] = 999
        write_json(path, payload)
        with pytest.raises(IncompatibleArtifactError, match="not a valid preview stage"):
            read_stage_config(previews)


class TestUnfinishedReview:
    def test_an_incomplete_session_cannot_be_recorded_as_a_stage(self, tmp_path: Path) -> None:
        selection = shortlist()
        plan = ReviewPlan(
            candidates=tuple(selection.candidates),
            analysis_fingerprint=FINGERPRINT,
            source_duration_seconds=selection.source_duration_seconds,
        )
        session = ReviewSession(
            tmp_path,
            plan,
            ReviewDecisionCollection(
                analysis_fingerprint=FINGERPRINT,
                source_duration_seconds=selection.source_duration_seconds,
                created_at=GENERATED_AT,
                updated_at=GENERATED_AT,
                decisions=[],
            ),
        )
        with pytest.raises(IncompatibleArtifactError, match="not finished"):
            session.stage_record()

    def test_a_complete_session_can_be(self, tmp_path: Path) -> None:
        selection = shortlist()
        candidate = selection.candidates[0]
        plan = ReviewPlan(
            candidates=tuple(selection.candidates),
            analysis_fingerprint=FINGERPRINT,
            source_duration_seconds=selection.source_duration_seconds,
        )
        session = ReviewSession(
            tmp_path,
            plan,
            ReviewDecisionCollection(
                analysis_fingerprint=FINGERPRINT,
                source_duration_seconds=selection.source_duration_seconds,
                created_at=GENERATED_AT,
                updated_at=GENERATED_AT,
                decisions=[
                    ApprovedDecision(
                        candidate_id=candidate.id,
                        original_start=candidate.start,
                        original_end=candidate.end,
                        final_start=candidate.start,
                        final_end=candidate.end,
                        reviewed_at=GENERATED_AT,
                    )
                ],
            ),
        )
        fingerprint, digest = session.stage_record()
        assert len(fingerprint) == 64
        assert len(digest) == 64


class TestManifestGuardsAtTheCommandBoundary:
    """A manifest that names something this build did not produce is refused."""

    def rewrite_manifest(self, run: Analysed, mutate: Any) -> None:
        path = run.run_path.joinpath("manifest.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def preview(self, run: Analysed, *arguments: str) -> Result:
        return runner.invoke(cli.app, ["preview", run.run_id, *arguments])

    def test_previewing_a_run_with_no_recorded_analysis_is_refused(
        self, analysed: Analysed
    ) -> None:
        self.rewrite_manifest(analysed, lambda payload: payload["stages"].pop("analysis"))
        result = self.preview(analysed)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "no recorded analysis" in cli_output(result)

    def test_previewing_candidates_of_another_schema_is_refused(self, analysed: Analysed) -> None:
        self.rewrite_manifest(
            analysed,
            lambda payload: payload["stages"]["analysis"].update({"schema_version": 99}),
        )
        result = self.preview(analysed)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "schema 99" in cli_output(result)

    def test_reusing_previews_with_no_recorded_fingerprint_is_refused(
        self, analysed: Analysed
    ) -> None:
        assert self.preview(analysed).exit_code == EXIT_SUCCESS
        self.rewrite_manifest(
            analysed, lambda payload: payload["stages"].pop(RunStage.PREVIEW.value)
        )
        result = self.preview(analysed)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "no fingerprint was recorded" in cli_output(result)

    def test_reusing_previews_of_another_index_schema_is_refused(self, analysed: Analysed) -> None:
        assert self.preview(analysed).exit_code == EXIT_SUCCESS
        self.rewrite_manifest(
            analysed,
            lambda payload: payload["stages"][RunStage.PREVIEW.value].update(
                {"schema_version": 99}
            ),
        )
        result = self.preview(analysed)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "index schema 99" in cli_output(result)

    def test_reviewing_a_run_with_no_recorded_previews_is_refused(self, analysed: Analysed) -> None:
        assert self.preview(analysed).exit_code == EXIT_SUCCESS
        self.rewrite_manifest(
            analysed, lambda payload: payload["stages"].pop(RunStage.PREVIEW.value)
        )
        result = runner.invoke(cli.app, ["review", analysed.run_id], input="a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "no recorded previews" in cli_output(result)

    def test_reviewing_previews_of_another_index_schema_is_refused(
        self, analysed: Analysed
    ) -> None:
        assert self.preview(analysed).exit_code == EXIT_SUCCESS
        self.rewrite_manifest(
            analysed,
            lambda payload: payload["stages"][RunStage.PREVIEW.value].update(
                {"schema_version": 99}
            ),
        )
        result = runner.invoke(cli.app, ["review", analysed.run_id], input="a\n")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "index schema 99" in cli_output(result)


def test_the_fake_media_harness_is_used_by_these_tests(media: FakeMedia) -> None:
    """Guards against the fixture being dropped from the module by accident."""
    assert media.calls == []
