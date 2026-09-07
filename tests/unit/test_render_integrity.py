"""Every way a set of clips can be damaged, and the refusal it earns.

A digest that still looks right proves nothing if the artifact it addresses was
replaced, so verification reads each file back and re-validates it under its own
schema. These are the paths through that reading -- a file that will not parse,
one written by a build with another schema, one whose contents no longer satisfy
its model -- and each one has to become a refusal that names ``--force`` rather
than an exception from deep inside pydantic.

The generation-side refusals are here too. An index or a clip record the stage
cannot describe is a bug in this code, not a damaged artifact, but it still has
to leave the previous set alone and reach the caller as a render failure rather
than as an internal fault.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.media.render import FFmpegClipRenderer
from content_engine.domain.exceptions import IncompatibleArtifactError, RenderError
from content_engine.domain.models import TranscriptSegment, TranscriptWord
from content_engine.domain.render_rules import (
    RENDER_INDEX_FILENAME,
    RENDER_STAGE_CONFIG_FILENAME,
)
from content_engine.domain.renders import (
    CLIP_METADATA_FILENAME,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
)
from content_engine.services import render_service
from content_engine.services.render_service import (
    RenderPlan,
    RenderService,
    read_index,
    read_stage_config,
    require_clips,
)
from tests.conftest import FakeMedia
from tests.unit.test_render_service import GENERATED_AT, plan_for


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    return FakeMedia(width=1080, height=1920).install(monkeypatch)


def service() -> RenderService:
    return RenderService(FFmpegClipRenderer(), FFprobeAdapter())


@pytest.fixture
def rendered(media: FakeMedia, tmp_path: Path) -> tuple[Path, RenderPlan, str, str]:
    directory = tmp_path.joinpath("clips")
    plan = plan_for(tmp_path, count=1)
    outcome = service().generate(plan, directory, GENERATED_AT)
    return directory, plan, outcome.fingerprint, outcome.stage_config_sha256


def prove(rendered: tuple[Path, RenderPlan, str, str]) -> None:
    directory, plan, fingerprint, digest = rendered
    require_clips(directory, fingerprint, digest, plan.target)


class TestTheIndex:
    def test_a_missing_index_is_refused(self, rendered: tuple[Path, RenderPlan, str, str]) -> None:
        directory, _, _, _ = rendered
        directory.joinpath(RENDER_INDEX_FILENAME).unlink()

        with pytest.raises(IncompatibleArtifactError, match="missing"):
            read_index(directory)

    def test_an_index_that_is_not_json_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        directory.joinpath(RENDER_INDEX_FILENAME).write_text("{not json", encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="cannot be read"):
            read_index(directory)

    def test_an_index_holding_a_list_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        directory.joinpath(RENDER_INDEX_FILENAME).write_text("[]", encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="does not contain"):
            read_index(directory)

    def test_an_index_of_another_schema_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        path = directory.joinpath(RENDER_INDEX_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["schema_version"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="schema 99"):
            read_index(directory)

    def test_an_index_that_no_longer_validates_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        path = directory.joinpath(RENDER_INDEX_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["source_duration_seconds"] = -1.0
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="not a valid render index"):
            read_index(directory)


class TestTheStageConfiguration:
    def test_a_missing_configuration_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        directory.joinpath(RENDER_STAGE_CONFIG_FILENAME).unlink()

        with pytest.raises(IncompatibleArtifactError, match="missing"):
            read_stage_config(directory)

    def test_a_configuration_of_another_schema_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        path = directory.joinpath(RENDER_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["schema_version"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="schema 99"):
            read_stage_config(directory)

    def test_a_configuration_that_no_longer_validates_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        directory, _, _, _ = rendered
        path = directory.joinpath(RENDER_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["crf"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="not a valid render stage"):
            read_stage_config(directory)


class TestTheMetadata:
    def clip_directory(self, rendered: tuple[Path, RenderPlan, str, str]) -> Path:
        directory, _, _, _ = rendered
        return directory.joinpath(read_index(directory).clips[0].directory)

    def test_a_missing_metadata_file_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        self.clip_directory(rendered).joinpath(CLIP_METADATA_FILENAME).unlink()

        with pytest.raises(IncompatibleArtifactError, match="missing"):
            prove(rendered)

    def test_metadata_of_another_schema_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        path = self.clip_directory(rendered).joinpath(CLIP_METADATA_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["schema_version"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="metadata schema 99"):
            prove(rendered)

    def test_metadata_that_no_longer_validates_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        path = self.clip_directory(rendered).joinpath(CLIP_METADATA_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["total_score"] = 5000.0
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match="not valid clip metadata"):
            prove(rendered)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("rank", 9), ("cue_count", 999), ("video_codec", "hevc"), ("sha256", "f" * 64)],
    )
    def test_metadata_disagreeing_with_the_index_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str], field: str, value: Any
    ) -> None:
        path = self.clip_directory(rendered).joinpath(CLIP_METADATA_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(IncompatibleArtifactError, match=field):
            prove(rendered)


class TestTheSubtitleDocuments:
    def clip_directory(self, rendered: tuple[Path, RenderPlan, str, str]) -> Path:
        directory, _, _, _ = rendered
        return directory.joinpath(read_index(directory).clips[0].directory)

    def rehash(self, rendered: tuple[Path, RenderPlan, str, str], name: str) -> None:
        """Make the index and the metadata agree with the damaged file.

        Without this the digest check refuses it first, which is correct and is
        asserted elsewhere -- but it means the parse never runs. The point of
        these tests is the layer *behind* the digests: somebody who edited a
        subtitle file and carefully updated every hash still cannot publish a
        document a player would reject.
        """
        from content_engine.utils.hashing import sha256_file

        directory, _, _, _ = rendered
        clip_directory = self.clip_directory(rendered)
        path = clip_directory.joinpath(name)
        key = "srt" if name.endswith(".srt") else "ass"
        digest = sha256_file(path)

        index_path = directory.joinpath(RENDER_INDEX_FILENAME)
        payload = json.loads(index_path.read_text("utf-8"))
        payload["clips"][0][f"{key}_sha256"] = digest
        payload["clips"][0][f"{key}_size_bytes"] = path.stat().st_size
        index_path.write_text(json.dumps(payload), encoding="utf-8")

        metadata_path = clip_directory.joinpath(CLIP_METADATA_FILENAME)
        metadata = json.loads(metadata_path.read_text("utf-8"))
        metadata[f"{key}_sha256"] = digest
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def test_an_unparseable_srt_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        self.clip_directory(rendered).joinpath(SUBTITLES_SRT_FILENAME).write_text(
            "1\nnot a timestamp\ntexto\n", encoding="utf-8"
        )
        self.rehash(rendered, SUBTITLES_SRT_FILENAME)

        with pytest.raises(RenderError, match="cannot be read back"):
            prove(rendered)

    def test_an_unparseable_ass_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        self.clip_directory(rendered).joinpath(SUBTITLES_ASS_FILENAME).write_text(
            "[Events]\nDialogue: 0,nonsense,0:00:01.00,X,,0,0,0,,texto\n", encoding="utf-8"
        )
        self.rehash(rendered, SUBTITLES_ASS_FILENAME)

        with pytest.raises(RenderError, match="cannot be read back"):
            prove(rendered)

    def test_an_event_past_the_end_of_the_clip_is_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        self.clip_directory(rendered).joinpath(SUBTITLES_SRT_FILENAME).write_text(
            "1\n00:59:00,000 --> 00:59:01,000\ntarde\n", encoding="utf-8"
        )
        self.rehash(rendered, SUBTITLES_SRT_FILENAME)

        with pytest.raises(RenderError, match="past the"):
            prove(rendered)

    def test_events_out_of_order_are_refused(
        self, rendered: tuple[Path, RenderPlan, str, str]
    ) -> None:
        self.clip_directory(rendered).joinpath(SUBTITLES_SRT_FILENAME).write_text(
            "1\n00:00:05,000 --> 00:00:06,000\nsegundo\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nprimero\n",
            encoding="utf-8",
        )
        self.rehash(rendered, SUBTITLES_SRT_FILENAME)

        with pytest.raises(RenderError, match="starting before"):
            prove(rendered)


class TestGenerationRefusals:
    def test_a_clip_record_the_stage_cannot_describe_becomes_a_render_error(
        self, media: FakeMedia, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pydantic error reaching the CLI would read as an internal fault.

        The failure is injected rather than provoked, and that is the finding
        rather than a shortcut. ``_measure`` checks everything the model checks
        -- dimensions, both codecs, the sample aspect ratio, the duration
        against the same tolerance -- so no probe result can reach the
        constructor and fail it. An earlier version of this test set a measured
        duration of zero and asserted a bare ``RenderError``; it passed on the
        message from the *probe*, three checks earlier, and would have gone on
        passing with this translation deleted.

        What is worth pinning is the translation itself: a ValidationError here
        must arrive as a render failure naming the stage, never as an unexpected
        internal fault in front of an operator.
        """
        real = render_service.ClipRecord

        def refuse(**fields: Any) -> Any:
            del fields
            return real(candidate_id="")  # every other required field is missing

        monkeypatch.setattr(render_service, "ClipRecord", refuse)

        engine = service()
        plan = plan_for(tmp_path, count=1)
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="clip it cannot describe"):
            engine.generate(plan, clips, GENERATED_AT)

    def test_an_index_the_stage_cannot_describe_becomes_a_render_error(
        self, media: FakeMedia, tmp_path: Path
    ) -> None:
        """A clip reaching past the source is refused by the index, not written."""
        plan = plan_for(tmp_path, count=1)
        broken = replace(plan.target, source_duration_seconds=0.5)
        impossible = RenderPlan(
            target=broken,
            config=plan.config,
            source_path=plan.source_path,
            transcript=plan.transcript,
            run_id=plan.run_id,
        )

        engine = service()
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="cannot describe"):
            engine.generate(impossible, clips, GENERATED_AT)

    def test_records_that_disagree_with_the_target_become_a_render_error(
        self, media: FakeMedia, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = plan_for(tmp_path, count=1)
        monkeypatch.setattr(
            render_service,
            "render_coherence_problem",
            lambda index, config, target: "a synthetic disagreement",
        )

        engine = service()
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="records that disagree"):
            engine.generate(plan, clips, GENERATED_AT)

    def test_a_silent_clip_is_refused_by_the_probe_adapter(
        self, media: FakeMedia, tmp_path: Path
    ) -> None:
        """With FFprobeAdapter underneath, a silent clip cannot even be read back.

        The adapter raises NoAudioStreamError, so the refusal arrives from
        ``_probe_clip``. Asserted for the message it really produces: matching
        "no audio stream" here would pass on the adapter's own words and prove
        nothing about the service.
        """
        media.audio = False

        engine = service()
        plan = plan_for(tmp_path, count=1)
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="cannot be read back"):
            engine.generate(plan, clips, GENERATED_AT)

    def test_a_probe_reporting_no_audio_codec_is_refused_by_the_service(
        self, media: FakeMedia, tmp_path: Path
    ) -> None:
        """Reachable through the port, not through FFprobeAdapter.

        The service holds a Protocol rather than the adapter, and a MediaInfo
        with no audio codec is a legal answer under it. So the service checks
        rather than assuming whichever probe it is given will keep refusing on
        its behalf -- and the preview stage has already been bitten once by
        trusting what an encoder produced without looking at the audio.

        The ``media`` fixture is still needed: it fakes the *encoder*, and only
        the probe is replaced here.
        """
        from content_engine.domain.models import MediaInfo
        from content_engine.ports.preview import MediaProbePort

        class SilentProbe:
            def probe(self, input_path: Path) -> tuple[MediaInfo, dict[str, Any]]:
                del input_path
                return (
                    MediaInfo(
                        duration_seconds=29.0,
                        video_codec="h264",
                        width=1080,
                        height=1920,
                        fps=25.0,
                        sample_aspect_ratio="1:1",
                        audio_codec=None,
                        sample_rate=None,
                        channels=None,
                        container="mov,mp4,m4a",
                        file_size=1024,
                    ),
                    {},
                )

        probe: MediaProbePort = SilentProbe()
        engine = RenderService(FFmpegClipRenderer(), probe)

        plan = plan_for(tmp_path, count=1)
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="A silent clip is not publishable"):
            engine.generate(plan, clips, GENERATED_AT)

    def test_subtitles_that_cannot_be_built_become_a_render_error(
        self, media: FakeMedia, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            render_service,
            "build_cues",
            lambda *arguments, **keywords: (_ for _ in ()).throw(ValueError("synthetic")),
        )

        engine = service()
        plan = plan_for(tmp_path, count=1)
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="cannot be built"):
            engine.generate(plan, clips, GENERATED_AT)


class TestTheAdapter:
    def test_an_encoder_that_reports_success_and_writes_nothing_is_refused(
        self, media: FakeMedia, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not theoretical. On Windows FFmpeg exits 0 past MAX_PATH and writes no file.

        Observed at 268 characters while testing the preview wheel from a deeply
        nested directory. Catching it in the adapter is what turns it into a
        named failure rather than a confusing one three steps later, when a file
        that does not exist is probed.
        """
        from content_engine.adapters.media import render as render_adapter
        from tests.conftest import fake_process

        monkeypatch.setattr(
            render_adapter, "run_command", lambda arguments, **_: fake_process(arguments)
        )

        engine = service()
        plan = plan_for(tmp_path, count=1)
        clips = tmp_path.joinpath("clips")

        with pytest.raises(RenderError, match="produced no clip"):
            engine.generate(plan, clips, GENERATED_AT)


class TestSilence:
    def test_a_clip_over_a_stretch_with_no_words_gets_empty_subtitles(
        self, media: FakeMedia, tmp_path: Path
    ) -> None:
        """An interval with nothing said in it is a real clip, not a failure."""
        plan = plan_for(tmp_path, count=1)
        silent = plan.transcript.model_copy(
            update={
                "segments": [
                    TranscriptSegment(
                        index=segment.index,
                        start=segment.start,
                        end=segment.end,
                        text=segment.text,
                        words=[
                            TranscriptWord(word="lejos", start=115.0, end=115.5, probability=0.9)
                        ]
                        if segment.index == len(plan.transcript.segments) - 1
                        else [],
                    )
                    for segment in plan.transcript.segments
                ]
            }
        )
        quiet = RenderPlan(
            target=plan.target,
            config=plan.config,
            source_path=plan.source_path,
            transcript=silent,
            run_id=plan.run_id,
        )
        directory = tmp_path.joinpath("clips")

        outcome = service().generate(quiet, directory, GENERATED_AT)

        clip = outcome.index.clips[0]
        assert clip.cue_count == 0
        assert clip.srt_size_bytes == 0
        base = directory.joinpath(clip.directory)
        assert base.joinpath(SUBTITLES_SRT_FILENAME).read_text("utf-8") == ""
        assert "[Events]" in base.joinpath(SUBTITLES_ASS_FILENAME).read_text("utf-8")
