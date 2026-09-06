"""CE-034: generating, verifying and reusing a set of previews.

The service is exercised through the real adapters with a fake encoder behind
them, so the arguments asserted elsewhere are the arguments that run here. What
these tests are about is everything around the encode: that a failure leaves no
artifact, that a finished set can be proved to still match its inputs, and that
proving it rewrites nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.media.preview import FFmpegPreviewRenderer
from content_engine.domain.candidates import CandidateCollection
from content_engine.domain.exceptions import (
    IncompatibleArtifactError,
    RenderError,
)
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    PREVIEW_STAGE_CONFIG_FILENAME,
    preview_filename,
    preview_stage_config,
)
from content_engine.services.preview_service import (
    STAGING_DIRNAME,
    PreviewPlan,
    PreviewService,
    read_index,
    verify_previews,
)
from tests.conftest import (
    FakeMedia,
    chunk_of,
    collect,
    raw_candidate,
    speech_transcript,
    weak_candidate,
)

FINGERPRINT = "a" * 64
GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("clase de vídeo ñandú.mp4")
    path.write_bytes(b"source-video")
    return path


def shortlist() -> CandidateCollection:
    return collect(
        chunk_of(speech_transcript()),
        [raw_candidate(10.0, 39.0), raw_candidate(60.0, 89.0, hook=88)],
    )


def plan_for(source: Path, collection: CandidateCollection | None = None) -> PreviewPlan:
    selection = collection or shortlist()
    return PreviewPlan(
        candidates=tuple(selection.candidates),
        config=preview_stage_config(width=540, height=960),
        analysis_fingerprint=FINGERPRINT,
        source_path=source,
        source_sha256="c" * 64,
        source_duration_seconds=selection.source_duration_seconds,
    )


def service() -> PreviewService:
    return PreviewService(FFmpegPreviewRenderer(), FFprobeAdapter())


class TestGeneration:
    def test_one_preview_per_selected_candidate(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        outcome = service().generate(plan, tmp_path.joinpath("previews"), GENERATED_AT)
        assert len(outcome.index.previews) == 2
        for candidate in plan.candidates:
            assert tmp_path.joinpath("previews", preview_filename(candidate.id)).is_file()

    def test_the_encoder_is_asked_for_the_candidate_interval(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        service().generate(plan, tmp_path.joinpath("previews"), GENERATED_AT)
        for call, candidate in zip(media.calls, plan.candidates, strict=True):
            assert call[call.index("-ss") + 1] == f"{candidate.start:.3f}"
            assert call[call.index("-t") + 1] == f"{candidate.duration:.3f}"

    def test_the_source_is_passed_as_one_argument(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        service().generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)
        assert media.calls[0][media.calls[0].index("-i") + 1] == str(source)

    def test_the_index_records_what_ffprobe_measured(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        outcome = service().generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)
        first = outcome.index.previews[0]
        assert (first.width, first.height) == (540, 960)
        assert first.video_codec == "h264"
        assert first.audio_codec == "aac"
        assert first.size_bytes > 0
        assert len(first.sha256) == 64

    def test_both_artifacts_are_written(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        service().generate(plan_for(source), directory, GENERATED_AT)
        assert directory.joinpath(PREVIEW_INDEX_FILENAME).is_file()
        assert directory.joinpath(PREVIEW_STAGE_CONFIG_FILENAME).is_file()

    def test_nothing_temporary_survives(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        service().generate(plan_for(source), directory, GENERATED_AT)
        assert not directory.joinpath(STAGING_DIRNAME).exists()
        assert [path.name for path in directory.rglob("*.tmp")] == []

    def test_zero_candidates_produce_an_honest_empty_index(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        empty = collect(chunk_of(speech_transcript()), [weak_candidate(10.0, 39.0)])
        assert empty.candidates == []
        directory = tmp_path.joinpath("previews")
        outcome = service().generate(plan_for(source, empty), directory, GENERATED_AT)
        assert outcome.index.previews == []
        assert media.calls == []
        assert directory.joinpath(PREVIEW_INDEX_FILENAME).is_file()

    def test_a_missing_source_is_refused_before_any_encode(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        source.unlink()
        with pytest.raises(RenderError, match="source"):
            service().generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)
        assert media.calls == []


class TestFailuresLeaveNothingBehind:
    def test_an_encoder_failure_produces_no_preview(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        media.fail_for = {preview_filename(plan.candidates[1].id)}
        directory = tmp_path.joinpath("previews")
        with pytest.raises(RenderError):
            service().generate(plan, directory, GENERATED_AT)
        survivors = sorted(path.name for path in directory.rglob("*")) if directory.exists() else []
        assert survivors == []

    def test_a_preview_of_the_wrong_size_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        media.dimensions = {preview_filename(plan.candidates[0].id): (360, 640)}
        directory = tmp_path.joinpath("previews")
        with pytest.raises(RenderError, match="540x960"):
            service().generate(plan, directory, GENERATED_AT)
        assert not directory.joinpath(PREVIEW_INDEX_FILENAME).exists()

    def test_a_preview_of_the_wrong_length_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        media.measured = {preview_filename(plan.candidates[0].id): 3.0}
        with pytest.raises(RenderError, match="duration"):
            service().generate(plan, tmp_path.joinpath("previews"), GENERATED_AT)

    def test_a_preview_without_audio_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        media.audio = False
        with pytest.raises(RenderError, match="audio"):
            service().generate(plan_for(source), tmp_path.joinpath("previews"), GENERATED_AT)

    def test_a_failed_regeneration_keeps_the_previous_previews(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        plan = plan_for(source)
        directory = tmp_path.joinpath("previews")
        service().generate(plan, directory, GENERATED_AT)
        before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}

        media.fail_for = {preview_filename(plan.candidates[1].id)}
        with pytest.raises(RenderError):
            service().generate(plan, directory, GENERATED_AT)
        after = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
        assert after == before


class TestVerification:
    def generated(self, directory: Path, source: Path) -> tuple[PreviewPlan, str, str]:
        plan = plan_for(source)
        outcome = service().generate(plan, directory, GENERATED_AT)
        return plan, outcome.fingerprint, outcome.stage_config_sha256

    def test_an_untouched_set_verifies(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        assert verify_previews(directory, fingerprint, digest, plan) == read_index(directory)

    def test_verification_writes_nothing(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        before = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
        verify_previews(directory, fingerprint, digest, plan)
        assert {path.name: path.read_bytes() for path in sorted(directory.iterdir())} == before

    def test_a_deleted_preview_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        directory.joinpath(preview_filename(plan.candidates[0].id)).unlink()
        with pytest.raises(IncompatibleArtifactError, match="missing"):
            verify_previews(directory, fingerprint, digest, plan)

    def test_an_edited_preview_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        directory.joinpath(preview_filename(plan.candidates[0].id)).write_bytes(b"something else")
        with pytest.raises(IncompatibleArtifactError, match="changed"):
            verify_previews(directory, fingerprint, digest, plan)

    def test_a_truncated_preview_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        path = directory.joinpath(preview_filename(plan.candidates[0].id))
        path.write_bytes(path.read_bytes()[:3])
        with pytest.raises(IncompatibleArtifactError, match="changed"):
            verify_previews(directory, fingerprint, digest, plan)

    def test_a_missing_index_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        directory.joinpath(PREVIEW_INDEX_FILENAME).unlink()
        with pytest.raises(IncompatibleArtifactError, match=PREVIEW_INDEX_FILENAME):
            verify_previews(directory, fingerprint, digest, plan)

    def test_an_edited_index_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        path = directory.joinpath(PREVIEW_INDEX_FILENAME)
        path.write_text(
            path.read_text(encoding="utf-8").replace("2026-03-01", "2025-03-01"),
            encoding="utf-8",
        )
        with pytest.raises(IncompatibleArtifactError, match="fingerprint"):
            verify_previews(directory, fingerprint, digest, plan)

    def test_an_edited_stage_configuration_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        path = directory.joinpath(PREVIEW_STAGE_CONFIG_FILENAME)
        path.write_text(
            path.read_text(encoding="utf-8").replace('"crf": 30', '"crf": 18'), encoding="utf-8"
        )
        with pytest.raises(IncompatibleArtifactError, match="configuration"):
            verify_previews(directory, fingerprint, digest, plan)

    def test_different_settings_are_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        smaller = PreviewPlan(
            candidates=plan.candidates,
            config=preview_stage_config(width=360, height=640),
            analysis_fingerprint=plan.analysis_fingerprint,
            source_path=plan.source_path,
            source_sha256=plan.source_sha256,
            source_duration_seconds=plan.source_duration_seconds,
        )
        with pytest.raises(IncompatibleArtifactError, match="settings"):
            verify_previews(directory, fingerprint, digest, smaller)

    def test_a_changed_shortlist_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        fewer = PreviewPlan(
            candidates=plan.candidates[:1],
            config=plan.config,
            analysis_fingerprint=plan.analysis_fingerprint,
            source_path=plan.source_path,
            source_sha256=plan.source_sha256,
            source_duration_seconds=plan.source_duration_seconds,
        )
        with pytest.raises(IncompatibleArtifactError):
            verify_previews(directory, fingerprint, digest, fewer)

    def test_another_analysis_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, fingerprint, digest = self.generated(directory, source)
        other = PreviewPlan(
            candidates=plan.candidates,
            config=plan.config,
            analysis_fingerprint="z" * 64,
            source_path=plan.source_path,
            source_sha256=plan.source_sha256,
            source_duration_seconds=plan.source_duration_seconds,
        )
        with pytest.raises(IncompatibleArtifactError, match="analysis"):
            verify_previews(directory, fingerprint, digest, other)

    def test_a_recorded_fingerprint_that_cannot_be_rebuilt_is_refused(
        self, tmp_path: Path, source: Path, media: FakeMedia
    ) -> None:
        directory = tmp_path.joinpath("previews")
        plan, _, digest = self.generated(directory, source)
        with pytest.raises(IncompatibleArtifactError, match="fingerprint"):
            verify_previews(directory, "f" * 64, digest, plan)
