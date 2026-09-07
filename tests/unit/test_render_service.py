"""Generating, publishing and proving a set of clips (CE-045, CE-046).

The service is the part that has to be right about files rather than about
arithmetic, so these tests are about what ends up on disk: that nothing reaches
the clips directory until every clip has been probed, that each clip directory
holds exactly the four artifacts the specification names, that a record is a
measurement of the finished file rather than a copy of the request, and that a
later invocation can prove all of it without an encoder.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.media.render import FFmpegClipRenderer
from content_engine.config import load_settings
from content_engine.domain.candidates import ValidatedCandidate
from content_engine.domain.enums import ReviewDecisionType
from content_engine.domain.exceptions import IncompatibleArtifactError, RenderError
from content_engine.domain.models import Transcript
from content_engine.domain.render_rules import (
    RENDER_INDEX_FILENAME,
    RENDER_STAGE_CONFIG_FILENAME,
    render_stage_config,
)
from content_engine.domain.render_targets import build_render_target
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
    clip_dirname,
)
from content_engine.domain.review import (
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
    ReviewDecisionCollection,
)
from content_engine.domain.subtitles import read_ass_events, read_srt_events
from content_engine.services.render_service import (
    STAGING_DIRNAME,
    RenderPlan,
    RenderService,
    require_clips,
    verify_clips,
)
from tests.conftest import (
    CANDIDATE_POLICY,
    FakeMedia,
    chunk_of,
    collect,
    raw_candidate,
    speech_transcript,
)

GENERATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def shortlist(transcript: Transcript, count: int) -> list[ValidatedCandidate]:
    chunk = chunk_of(transcript)
    proposals = [raw_candidate(index * 30.0, index * 30.0 + 29.0) for index in range(count)]
    collection = collect(chunk, proposals, CANDIDATE_POLICY, transcript.duration_seconds)
    assert len(collection.candidates) == count, collection.counts
    return collection.candidates


def approve(candidate: ValidatedCandidate) -> ApprovedDecision:
    return ApprovedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=GENERATED_AT,
        final_start=candidate.start,
        final_end=candidate.end,
    )


def reject(candidate: ValidatedCandidate) -> RejectedDecision:
    return RejectedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=GENERATED_AT,
    )


def edit(candidate: ValidatedCandidate, start: float, end: float) -> EditedDecision:
    return EditedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=GENERATED_AT,
        final_start=start,
        final_end=end,
    )


def plan_for(
    tmp_path: Path,
    decide: Any = approve,
    count: int = 2,
    *,
    words: bool = True,
    **config_overrides: Any,
) -> RenderPlan:
    transcript = speech_transcript(words=words)
    candidates = shortlist(transcript, count)
    collection = ReviewDecisionCollection(
        analysis_fingerprint=DIGEST,
        source_duration_seconds=transcript.duration_seconds,
        created_at=GENERATED_AT,
        updated_at=GENERATED_AT,
        decisions=[decide(candidate) for candidate in candidates],
    )
    target = build_render_target(
        candidates=tuple(candidates),
        decisions=collection,
        analysis_fingerprint=DIGEST,
        review_fingerprint="b" * 64,
        decisions_sha256="c" * 64,
        transcript_sha256="d" * 64,
        source_sha256="e" * 64,
        source_duration_seconds=transcript.duration_seconds,
    )
    source = tmp_path.joinpath("source.mp4")
    source.write_bytes(b"source")
    config = render_stage_config(load_settings().render)
    if config_overrides:
        config = type(config).model_validate(config.model_dump(mode="json") | config_overrides)
    return RenderPlan(
        target=target,
        config=config,
        source_path=source,
        transcript=transcript,
        run_id="run-under-test",
    )


def service(media: FakeMedia) -> RenderService:
    """The real service over the real adapters, with FFmpeg faked underneath.

    The adapters are not stubbed: the argument list the production code builds
    is the one the fake receives and the tests assert on, so the command under
    test cannot be a stand-in for itself. Real FFmpeg is exercised in
    tests/integration.
    """
    del media  # installed by the fixture; named so the dependency is visible
    return RenderService(FFmpegClipRenderer(), FFprobeAdapter())


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    """The shared fake, sized for a final render rather than a preview."""
    return FakeMedia(width=1080, height=1920).install(monkeypatch)


@pytest.fixture
def clips(tmp_path: Path) -> Path:
    return tmp_path.joinpath("clips")


class TestGeneration:
    def test_one_directory_per_kept_candidate(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path)

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        assert len(outcome.index.clips) == 2
        for clip in outcome.index.clips:
            assert clips.joinpath(clip.directory).is_dir()

    def test_each_directory_holds_exactly_the_four_artifacts(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path), clips, GENERATED_AT)

        for clip in outcome.index.clips:
            names = sorted(path.name for path in clips.joinpath(clip.directory).iterdir())
            assert names == sorted(
                (
                    CLIP_FILENAME,
                    CLIP_METADATA_FILENAME,
                    SUBTITLES_ASS_FILENAME,
                    SUBTITLES_SRT_FILENAME,
                )
            )

    def test_the_stage_writes_its_index_and_configuration(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        service(media).generate(plan_for(tmp_path), clips, GENERATED_AT)

        assert clips.joinpath(RENDER_INDEX_FILENAME).is_file()
        assert clips.joinpath(RENDER_STAGE_CONFIG_FILENAME).is_file()

    def test_a_rejected_candidate_gets_no_directory(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        transcript = speech_transcript()
        candidates = shortlist(transcript, 2)
        collection = ReviewDecisionCollection(
            analysis_fingerprint=DIGEST,
            source_duration_seconds=transcript.duration_seconds,
            created_at=GENERATED_AT,
            updated_at=GENERATED_AT,
            decisions=[reject(candidates[0]), approve(candidates[1])],
        )
        target = build_render_target(
            candidates=tuple(candidates),
            decisions=collection,
            analysis_fingerprint=DIGEST,
            review_fingerprint="b" * 64,
            decisions_sha256="c" * 64,
            transcript_sha256="d" * 64,
            source_sha256="e" * 64,
            source_duration_seconds=transcript.duration_seconds,
        )
        source = tmp_path.joinpath("source.mp4")
        source.write_bytes(b"source")
        plan = RenderPlan(
            target=target,
            config=render_stage_config(load_settings().render),
            source_path=source,
            transcript=transcript,
            run_id="run-under-test",
        )

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        assert [clip.candidate_id for clip in outcome.index.clips] == [candidates[1].id]
        assert not clips.joinpath(clip_dirname(candidates[0].id)).exists()

    def test_an_edited_candidate_is_cut_over_its_final_bounds(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, lambda c: edit(c, c.start + 2.0, c.end - 3.0), count=1)

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        clip = outcome.index.clips[0]
        assert clip.decision is ReviewDecisionType.EDITED
        assert clip.duration == pytest.approx(clip.original_end - clip.original_start - 5.0)
        seek = media.calls[0][media.calls[0].index("-ss") + 1]
        assert seek == f"{clip.start:.3f}"

    def test_no_staging_directory_survives_a_successful_run(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        service(media).generate(plan_for(tmp_path), clips, GENERATED_AT)

        assert not clips.joinpath(STAGING_DIRNAME).exists()
        assert not any(path.suffix == ".tmp" for path in clips.rglob("*"))

    def test_a_review_that_kept_nothing_produces_an_empty_but_valid_stage(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        """Not a failure and not a fabricated clip: an honest empty result."""
        plan = plan_for(tmp_path, reject)

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        assert outcome.index.clips == []
        assert clips.joinpath(RENDER_INDEX_FILENAME).is_file()
        assert media.calls == []


class TestMeasurement:
    def test_the_record_holds_what_ffprobe_read_back(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        clip = outcome.index.clips[0]
        assert (clip.width, clip.height) == (1080, 1920)
        assert clip.video_codec == "h264"
        assert clip.audio_codec == "aac"
        assert clip.sample_aspect_ratio == "1:1"

    def test_the_digests_are_the_bytes_on_disk(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        from content_engine.utils.hashing import sha256_file

        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        clip = outcome.index.clips[0]
        directory = clips.joinpath(clip.directory)
        assert clip.sha256 == sha256_file(directory.joinpath(CLIP_FILENAME))
        assert clip.srt_sha256 == sha256_file(directory.joinpath(SUBTITLES_SRT_FILENAME))
        assert clip.ass_sha256 == sha256_file(directory.joinpath(SUBTITLES_ASS_FILENAME))

    def test_a_clip_of_the_wrong_size_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.dimensions[CLIP_FILENAME] = (720, 1280)

        with pytest.raises(RenderError, match="1080x1920 was requested"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_a_clip_with_the_wrong_video_codec_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.video_codec = "hevc"

        with pytest.raises(RenderError, match="hevc video"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_a_clip_with_the_wrong_audio_codec_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.audio_codec = "mp3"

        with pytest.raises(RenderError, match="mp3 audio"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_a_silent_clip_is_refused(self, media: FakeMedia, tmp_path: Path, clips: Path) -> None:
        media.audio = False

        with pytest.raises(RenderError, match="no audio stream"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_non_square_pixels_are_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        """1080x1920 with a 4:3 pixel ratio is not a vertical video."""
        media.sample_aspect_ratio = "4:3"

        with pytest.raises(RenderError, match="4:3"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_an_undeclared_sample_aspect_ratio_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.sample_aspect_ratio = None

        with pytest.raises(RenderError, match="square pixels"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_a_clip_of_the_wrong_duration_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.measured[CLIP_FILENAME] = 3.0

        with pytest.raises(RenderError, match="duration tolerance"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

    def test_a_clip_that_cannot_be_probed_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        media.unprobeable.add(CLIP_FILENAME)

        with pytest.raises(RenderError, match="cannot be read back"):
            service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)


class TestSubtitles:
    def test_the_srt_holds_clip_local_times(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        clip = outcome.index.clips[0]
        text = clips.joinpath(clip.directory, SUBTITLES_SRT_FILENAME).read_text(encoding="utf-8")
        events = read_srt_events(text)
        assert events
        assert events[0].start_ms >= 0
        assert events[-1].end_ms <= round(clip.duration * 1000)

    def test_the_two_documents_describe_the_same_cues(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        directory = clips.joinpath(outcome.index.clips[0].directory)
        srt = read_srt_events(directory.joinpath(SUBTITLES_SRT_FILENAME).read_text("utf-8"))
        ass = read_ass_events(directory.joinpath(SUBTITLES_ASS_FILENAME).read_text("utf-8"))
        assert [event.text for event in srt] == [event.text for event in ass]
        assert len(srt) == outcome.index.clips[0].cue_count

    def test_the_files_are_utf8_without_a_bom_and_lf_only(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        directory = clips.joinpath(outcome.index.clips[0].directory)
        for name in (SUBTITLES_SRT_FILENAME, SUBTITLES_ASS_FILENAME):
            raw = directory.joinpath(name).read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf")
            assert b"\r" not in raw
            raw.decode("utf-8")

    def test_the_ass_document_is_burned_in_when_the_profile_says_so(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        graph = media.calls[0][media.calls[0].index("-filter_complex") + 1]
        assert "ass=" in graph
        assert SUBTITLES_ASS_FILENAME in graph

    def test_nothing_is_burned_in_when_the_profile_says_not_to(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1, burn_subtitles=False)

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        graph = media.calls[0][media.calls[0].index("-filter_complex") + 1]
        assert "ass=" not in graph
        assert outcome.index.clips[0].subtitles_burned is False

    def test_the_sidecar_files_exist_even_when_nothing_is_burned_in(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1, burn_subtitles=False)

        outcome = service(media).generate(plan, clips, GENERATED_AT)

        directory = clips.joinpath(outcome.index.clips[0].directory)
        assert directory.joinpath(SUBTITLES_SRT_FILENAME).is_file()
        assert directory.joinpath(SUBTITLES_ASS_FILENAME).is_file()

    def test_a_transcript_with_no_word_timestamps_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        """The contract is stated rather than approximated from segment bounds."""
        plan = plan_for(tmp_path, count=1, words=False)

        with pytest.raises(RenderError, match="word_timestamps"):
            service(media).generate(plan, clips, GENERATED_AT)

    def test_a_transcript_with_no_word_timestamps_and_nothing_to_render_is_fine(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        """Nothing was kept, so no subtitle was ever going to be built."""
        plan = plan_for(tmp_path, reject, words=False)

        assert service(media).generate(plan, clips, GENERATED_AT).index.clips == []


class TestMetadata:
    def test_it_travels_with_the_clip_and_names_its_provenance(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        clip = outcome.index.clips[0]
        payload = json.loads(
            clips.joinpath(clip.directory, CLIP_METADATA_FILENAME).read_text("utf-8")
        )
        assert payload["candidate_id"] == clip.candidate_id
        assert payload["rank"] == clip.rank
        assert payload["sha256"] == clip.sha256
        assert payload["analysis_fingerprint"] == DIGEST
        assert payload["run_id"] == "run-under-test"
        assert payload["topic"]

    def test_it_carries_the_editorial_fields_the_analyzer_produced(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        outcome = service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)

        payload = json.loads(
            clips.joinpath(outcome.index.clips[0].directory, CLIP_METADATA_FILENAME).read_text(
                "utf-8"
            )
        )
        for field in ("category", "topic", "hook", "summary", "reason", "total_score"):
            assert field in payload


class TestVerification:
    def test_a_freshly_generated_set_verifies(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path)
        outcome = service(media).generate(plan, clips, GENERATED_AT)

        index = verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

        assert len(index.clips) == 2

    def test_verification_writes_nothing(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        before = {
            path.relative_to(clips).as_posix(): path.read_bytes()
            for path in sorted(clips.rglob("*"))
            if path.is_file()
        }

        verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

        after = {
            path.relative_to(clips).as_posix(): path.read_bytes()
            for path in sorted(clips.rglob("*"))
            if path.is_file()
        }
        assert after == before

    @pytest.mark.parametrize(
        "name", [CLIP_FILENAME, SUBTITLES_SRT_FILENAME, SUBTITLES_ASS_FILENAME]
    )
    def test_a_deleted_artifact_cannot_be_reused(
        self, media: FakeMedia, tmp_path: Path, clips: Path, name: str
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        clips.joinpath(outcome.index.clips[0].directory, name).unlink()

        with pytest.raises(IncompatibleArtifactError, match="--force"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    @pytest.mark.parametrize(
        "name", [CLIP_FILENAME, SUBTITLES_SRT_FILENAME, SUBTITLES_ASS_FILENAME]
    )
    def test_a_tampered_artifact_cannot_be_reused(
        self, media: FakeMedia, tmp_path: Path, clips: Path, name: str
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        path = clips.joinpath(outcome.index.clips[0].directory, name)
        path.write_bytes(path.read_bytes() + b"tampered")

        with pytest.raises(IncompatibleArtifactError, match="changed since"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_truncated_clip_cannot_be_reused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        clips.joinpath(outcome.index.clips[0].directory, CLIP_FILENAME).write_bytes(b"x")

        with pytest.raises(IncompatibleArtifactError, match="changed since"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_renamed_clip_directory_cannot_be_reused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        directory = clips.joinpath(outcome.index.clips[0].directory)
        directory.rename(clips.joinpath("clip_cand_somethingelse"))

        with pytest.raises(IncompatibleArtifactError):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_leftover_directory_from_an_earlier_shortlist_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        stale = clips.joinpath("clip_cand_fromanotherrun")
        stale.mkdir()
        stale.joinpath(CLIP_FILENAME).write_bytes(b"stale")

        with pytest.raises(IncompatibleArtifactError, match="not in the index"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_tampered_metadata_file_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        path = clips.joinpath(outcome.index.clips[0].directory, CLIP_METADATA_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["rank"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="metadata"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_an_edited_index_cannot_rebuild_the_fingerprint(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        path = clips.joinpath(RENDER_INDEX_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["generated_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="fingerprint"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_missing_index_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        clips.joinpath(RENDER_INDEX_FILENAME).unlink()

        with pytest.raises(IncompatibleArtifactError, match="missing"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_a_configuration_that_no_longer_matches_the_manifest_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        path = clips.joinpath(RENDER_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["crf"] = 30
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="does not match the manifest"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan)

    def test_settings_that_changed_since_are_refused_by_verify_but_not_by_require(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        wanted = plan_for(tmp_path, count=1, crf=30)

        assert require_clips(
            clips, outcome.fingerprint, outcome.stage_config_sha256, plan.target
        ).clips
        with pytest.raises(IncompatibleArtifactError, match="different settings"):
            verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, wanted)

    def test_clips_from_another_analysis_are_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        moved = replace(plan.target, analysis_fingerprint="9" * 64)

        with pytest.raises(IncompatibleArtifactError, match="analysis"):
            require_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, moved)

    def test_a_damaged_subtitle_file_is_refused(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        """The digest would still match if the index were edited with it."""
        plan = plan_for(tmp_path, count=1)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        directory = clips.joinpath(outcome.index.clips[0].directory)
        directory.joinpath(SUBTITLES_SRT_FILENAME).write_text("nonsense\n", encoding="utf-8")
        index_path = clips.joinpath(RENDER_INDEX_FILENAME)
        payload = json.loads(index_path.read_text("utf-8"))
        from content_engine.utils.hashing import sha256_file

        payload["clips"][0]["srt_sha256"] = sha256_file(directory.joinpath(SUBTITLES_SRT_FILENAME))
        payload["clips"][0]["srt_size_bytes"] = (
            directory.joinpath(SUBTITLES_SRT_FILENAME).stat().st_size
        )
        index_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError):
            require_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan.target)


class TestPublication:
    def test_a_failed_regeneration_leaves_the_previous_set_byte_identical(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path)
        outcome = service(media).generate(plan, clips, GENERATED_AT)
        before = {
            path.relative_to(clips).as_posix(): path.read_bytes()
            for path in sorted(clips.rglob("*"))
            if path.is_file()
        }
        media.fail_for.add(CLIP_FILENAME)

        with pytest.raises(RenderError):
            service(media).generate(plan, clips, LATER)

        after = {
            path.relative_to(clips).as_posix(): path.read_bytes()
            for path in sorted(clips.rglob("*"))
            if path.is_file()
        }
        assert after == before
        assert verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, plan).clips

    def test_a_regeneration_with_a_smaller_shortlist_removes_the_stale_directories(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        service(media).generate(plan_for(tmp_path, count=2), clips, GENERATED_AT)
        smaller = plan_for(tmp_path, count=1)

        outcome = service(media).generate(smaller, clips, LATER)

        directories = sorted(
            path.name for path in clips.iterdir() if path.is_dir() and not path.name.startswith(".")
        )
        assert directories == [outcome.index.clips[0].directory]

    def test_a_regeneration_with_a_larger_shortlist_adds_directories(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        service(media).generate(plan_for(tmp_path, count=1), clips, GENERATED_AT)
        larger = plan_for(tmp_path, count=3)

        outcome = service(media).generate(larger, clips, LATER)

        assert len(outcome.index.clips) == 3
        assert verify_clips(clips, outcome.fingerprint, outcome.stage_config_sha256, larger).clips

    def test_a_file_the_stage_does_not_own_survives_a_regeneration(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        service(media).generate(plan, clips, GENERATED_AT)
        note = clips.joinpath("notes.txt")
        note.write_text("operator notes", encoding="utf-8")

        service(media).generate(plan, clips, LATER)

        assert note.read_text(encoding="utf-8") == "operator notes"


class TestRefusals:
    def test_a_missing_source_is_refused_before_anything_is_encoded(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        plan.source_path.unlink()

        with pytest.raises(RenderError, match="source is missing"):
            service(media).generate(plan, clips, GENERATED_AT)

        assert media.calls == []

    def test_a_missing_source_with_nothing_to_render_is_not_a_failure(
        self, media: FakeMedia, tmp_path: Path, clips: Path
    ) -> None:
        plan = plan_for(tmp_path, reject)
        plan.source_path.unlink()

        assert service(media).generate(plan, clips, GENERATED_AT).index.clips == []
