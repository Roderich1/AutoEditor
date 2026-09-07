"""``content-engine render`` from the command line (CE-040 to CE-046).

The service tests prove what ends up in a directory. These prove what the
command does around it: which run states it will act on, what it records in the
manifest, that a second invocation rewrites nothing at all, that a failure keeps
the run diagnosable, and that no path through it reads a credential or opens a
socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.config import ANALYSIS_CREDENTIAL_ENV_VAR
from content_engine.domain.enums import RunStatus
from content_engine.domain.exceptions import (
    EXIT_INVALID_INPUT,
    EXIT_RENDER,
    EXIT_SUCCESS,
)
from content_engine.domain.render_rules import (
    RENDER_INDEX_FILENAME,
    RENDER_STAGE_CONFIG_FILENAME,
)
from content_engine.domain.renders import (
    CLIP_FILENAME,
    CLIP_METADATA_FILENAME,
    SUBTITLES_ASS_FILENAME,
    SUBTITLES_SRT_FILENAME,
)
from tests.conftest import Analysed, FakeMedia, cli_output

RUNNER = CliRunner()


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    """The shared fake, answering at both preview and render dimensions.

    ``preview`` and ``render`` ask for different sizes in one test, so ffprobe
    has to answer for whichever file it is handed rather than for one constant.
    """
    fake = FakeMedia(width=540, height=960)
    fake.dimensions[CLIP_FILENAME] = (1080, 1920)
    return fake.install(monkeypatch)


def previewed(analysed: Analysed) -> Analysed:
    result = RUNNER.invoke(cli.app, ["preview", analysed.run_id])
    assert result.exit_code == EXIT_SUCCESS, cli_output(result)
    return analysed


def reviewed(analysed: Analysed, answers: str = "a\na\n") -> Analysed:
    previewed(analysed)
    result = RUNNER.invoke(cli.app, ["review", analysed.run_id], input=answers)
    assert result.exit_code == EXIT_SUCCESS, cli_output(result)
    assert analysed.manifest()["status"] == RunStatus.REVIEWED.value, cli_output(result)
    return analysed


def render(analysed: Analysed, *arguments: str) -> Any:
    return RUNNER.invoke(cli.app, ["render", analysed.run_id, *arguments])


def clip_snapshot(analysed: Analysed) -> dict[str, bytes]:
    directory = analysed.run_path.joinpath("clips")
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class TestHappyPath:
    def test_a_reviewed_run_renders_and_reaches_rendered(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "Clips ready" in cli_output(result)
        assert run.manifest()["status"] == RunStatus.RENDERED.value

    def test_the_manifest_records_the_render_stage(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS

        stage = run.manifest()["stages"]["render"]
        assert len(stage["fingerprint"]) == 64
        assert len(stage["stage_config_sha256"]) == 64
        assert stage["schema_version"] == 1
        assert stage["completed_at"]

    def test_the_recorded_digest_is_the_configuration_beside_the_clips(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        from content_engine.domain.render_rules import render_stage_config_sha256
        from content_engine.services.render_service import read_stage_config

        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS

        rebuilt = render_stage_config_sha256(read_stage_config(run.run_path.joinpath("clips")))
        assert rebuilt == run.manifest()["stages"]["render"]["stage_config_sha256"]

    def test_each_kept_candidate_gets_its_four_artifacts(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS

        index = json.loads(run.run_path.joinpath("clips", RENDER_INDEX_FILENAME).read_text("utf-8"))
        assert index["clips"]
        for clip in index["clips"]:
            directory = run.run_path.joinpath("clips", clip["directory"])
            for name in (
                CLIP_FILENAME,
                SUBTITLES_SRT_FILENAME,
                SUBTITLES_ASS_FILENAME,
                CLIP_METADATA_FILENAME,
            ):
                assert directory.joinpath(name).is_file()

    def test_a_rejected_candidate_is_not_rendered(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed, answers="a\nr\n\n\n")
        assert render(run).exit_code == EXIT_SUCCESS

        index = json.loads(run.run_path.joinpath("clips", RENDER_INDEX_FILENAME).read_text("utf-8"))
        decisions = {
            decision["candidate_id"]: decision["decision"]
            for decision in run.decisions()["decisions"]
        }
        rendered = {clip["candidate_id"] for clip in index["clips"]}
        assert rendered == {name for name, kind in decisions.items() if kind != "rejected"}

    def test_a_review_that_rejected_everything_still_reaches_rendered(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed, answers="r\n\n\nr\n\n\n")

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "kept none of the candidates" in cli_output(result)
        assert run.manifest()["status"] == RunStatus.RENDERED.value
        assert (
            json.loads(run.run_path.joinpath("clips", RENDER_INDEX_FILENAME).read_text("utf-8"))[
                "clips"
            ]
            == []
        )

    def test_nothing_temporary_is_left_behind(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS

        leftovers = [
            path
            for path in run.run_path.rglob("*")
            if path.suffix == ".tmp" or path.name in {".staging", ".rollback"}
        ]
        assert leftovers == []


class TestReuse:
    def test_a_second_invocation_reuses_and_rewrites_nothing(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        before = clip_snapshot(run)
        manifest_before = run.run_path.joinpath("manifest.json").read_bytes()
        encodes = len(media.calls)

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "Clips reused" in cli_output(result)
        assert clip_snapshot(run) == before
        assert run.run_path.joinpath("manifest.json").read_bytes() == manifest_before
        assert len(media.calls) == encodes, "reuse must not invoke FFmpeg"

    def test_reuse_needs_no_credential(
        self, analysed: Analysed, media: FakeMedia, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        before = clip_snapshot(run)
        monkeypatch.delenv(ANALYSIS_CREDENTIAL_ENV_VAR, raising=False)

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert clip_snapshot(run) == before

    def test_force_regenerates(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        encodes = len(media.calls)

        result = render(run, "--force")

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "Clips ready" in cli_output(result)
        assert len(media.calls) > encodes

    def test_force_produces_the_same_bytes_from_the_same_inputs(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        clips = {
            name: payload
            for name, payload in clip_snapshot(run).items()
            if name.endswith((".mp4", ".srt", ".ass"))
        }

        assert render(run, "--force").exit_code == EXIT_SUCCESS

        after = {
            name: payload
            for name, payload in clip_snapshot(run).items()
            if name.endswith((".mp4", ".srt", ".ass"))
        }
        assert after == clips


class TestRefusals:
    def test_a_run_that_has_not_been_reviewed_is_refused(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        previewed(analysed)

        result = render(analysed)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "review" in cli_output(result)
        assert not analysed.run_path.joinpath("clips", RENDER_INDEX_FILENAME).exists()

    def test_an_analysed_run_is_refused_before_anything_is_read(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        result = render(analysed)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert media.calls == []

    def test_an_incomplete_review_is_refused(self, analysed: Analysed, media: FakeMedia) -> None:
        """Skipping is not deciding, so there is nothing settled to render."""
        previewed(analysed)
        assert RUNNER.invoke(cli.app, ["review", analysed.run_id], input="a\ns\nq\n").exit_code == 0
        assert analysed.manifest()["status"] == RunStatus.READY_FOR_REVIEW.value

        result = render(analysed)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "review" in cli_output(result)

    def test_an_edited_decision_file_is_refused(self, analysed: Analysed, media: FakeMedia) -> None:
        """The one artifact that cannot be regenerated, so it is proved rather than read."""
        run = reviewed(analysed)
        path = run.review.joinpath("decisions.json")
        payload = json.loads(path.read_text("utf-8"))
        payload["decisions"][0]["decision"] = "rejected"
        payload["decisions"][0].pop("final_start")
        payload["decisions"][0].pop("final_end")
        payload["decisions"][0]["reason"] = None
        payload["decisions"][0]["detail"] = None
        path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "fingerprint" in cli_output(result)

    def test_a_deleted_preview_is_refused(self, analysed: Analysed, media: FakeMedia) -> None:
        """A decision was taken over a preview; if it is gone, it cannot be proved."""
        run = reviewed(analysed)
        next(run.previews.glob("candidate_*.mp4")).unlink()

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)

    def test_a_replaced_source_is_refused(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        source = Path(run.manifest()["input"]["path"])
        source.write_bytes(b"a different video entirely")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "not the one this run was created from" in cli_output(result)

    @pytest.mark.parametrize(
        "name", [CLIP_FILENAME, SUBTITLES_SRT_FILENAME, SUBTITLES_ASS_FILENAME]
    )
    def test_a_deleted_artifact_refuses_reuse_and_names_force(
        self, analysed: Analysed, media: FakeMedia, name: str
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        index = json.loads(run.run_path.joinpath("clips", RENDER_INDEX_FILENAME).read_text("utf-8"))
        run.run_path.joinpath("clips", index["clips"][0]["directory"], name).unlink()

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "--force" in cli_output(result)

    def test_a_tampered_clip_refuses_reuse(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        index = json.loads(run.run_path.joinpath("clips", RENDER_INDEX_FILENAME).read_text("utf-8"))
        path = run.run_path.joinpath("clips", index["clips"][0]["directory"], CLIP_FILENAME)
        path.write_bytes(path.read_bytes() + b"tampered")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "--force" in cli_output(result)

    def test_an_edited_index_refuses_reuse(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        path = run.run_path.joinpath("clips", RENDER_INDEX_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["generated_at"] = "2020-01-01T00:00:00Z"
        path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)

    def test_an_edited_stage_configuration_refuses_reuse(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        path = run.run_path.joinpath("clips", RENDER_STAGE_CONFIG_FILENAME)
        payload = json.loads(path.read_text("utf-8"))
        payload["crf"] = 30
        path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)

    def test_an_unknown_run_is_refused(self, settings: Any, media: FakeMedia) -> None:
        result = RUNNER.invoke(cli.app, ["render", "no-such-run"])

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)


class TestFailure:
    def test_a_failing_encoder_leaves_the_run_diagnosable(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        media.fail_for.add(CLIP_FILENAME)

        result = render(run)

        assert result.exit_code == EXIT_RENDER, cli_output(result)
        manifest = run.manifest()
        assert manifest["status"] == RunStatus.FAILED_RENDER.value
        assert manifest["failure"]["stage"] == "render"
        assert (
            "render" in manifest["failure"]["error_type"].lower() or manifest["failure"]["message"]
        )

    def test_a_failed_render_can_be_retried(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        media.fail_for.add(CLIP_FILENAME)
        assert render(run).exit_code == EXIT_RENDER
        media.fail_for.clear()

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert run.manifest()["status"] == RunStatus.RENDERED.value

    def test_a_failed_forced_render_destroys_nothing(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        before = clip_snapshot(run)
        media.fail_for.add(CLIP_FILENAME)

        assert render(run, "--force").exit_code == EXIT_RENDER

        assert clip_snapshot(run) == before

    def test_the_previous_set_still_verifies_after_a_failed_force(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        media.fail_for.add(CLIP_FILENAME)
        assert render(run, "--force").exit_code == EXIT_RENDER
        media.fail_for.clear()

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "recovered" in cli_output(result).lower() or "reused" in cli_output(result).lower()

    def test_a_failure_prints_no_traceback(self, analysed: Analysed, media: FakeMedia) -> None:
        run = reviewed(analysed)
        media.fail_for.add(CLIP_FILENAME)

        result = render(run)

        assert "Traceback" not in cli_output(result)


class TestIsolation:
    def test_no_credential_is_read_at_any_point(
        self, analysed: Analysed, media: FakeMedia, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        looked: list[str] = []
        import os

        real = os.environ.get

        def watched(key: str, default: Any = None) -> Any:
            looked.append(key)
            return real(key, default)

        run = reviewed(analysed)
        monkeypatch.setattr(os.environ, "get", watched)

        assert render(run).exit_code == EXIT_SUCCESS
        assert ANALYSIS_CREDENTIAL_ENV_VAR not in looked

    def test_no_credential_or_key_name_reaches_the_run_directory(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS

        needle = ANALYSIS_CREDENTIAL_ENV_VAR.encode()
        for path in run.run_path.rglob("*"):
            if path.is_file():
                assert needle not in path.read_bytes(), path

    def test_the_upstream_artifacts_are_left_byte_identical(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        watched = [
            *sorted(run.run_path.joinpath("analysis").rglob("*")),
            *sorted(run.run_path.joinpath("previews").rglob("*")),
            *sorted(run.run_path.joinpath("review").rglob("*")),
            *sorted(run.run_path.joinpath("transcript").rglob("*")),
        ]
        before = {path: path.read_bytes() for path in watched if path.is_file()}

        assert render(run).exit_code == EXIT_SUCCESS

        assert {path: path.read_bytes() for path in watched if path.is_file()} == before


class TestManifestConsistency:
    """The manifest and the directory have to agree before anything is reused.

    Each of these is a run whose files are intact and whose manifest says
    something else. None of them is recoverable by guessing, so each is a
    refusal that names what disagrees.
    """

    def test_a_run_with_no_recorded_review_is_refused(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        manifest_path = run.run_path.joinpath("manifest.json")
        payload = json.loads(manifest_path.read_text("utf-8"))
        payload["stages"].pop("review")
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "no recorded review" in cli_output(result)

    def test_decisions_of_another_schema_are_refused(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        manifest_path = run.run_path.joinpath("manifest.json")
        payload = json.loads(manifest_path.read_text("utf-8"))
        payload["stages"]["review"]["schema_version"] = 99
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "decisions use schema 99" in cli_output(result)

    def test_clips_with_no_recorded_fingerprint_are_refused(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        """The index is on disk and the manifest never recorded the stage."""
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        manifest_path = run.run_path.joinpath("manifest.json")
        payload = json.loads(manifest_path.read_text("utf-8"))
        payload["stages"].pop("render")
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "no fingerprint was recorded" in cli_output(result)

    def test_clips_of_another_index_schema_are_refused(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        assert render(run).exit_code == EXIT_SUCCESS
        manifest_path = run.run_path.joinpath("manifest.json")
        payload = json.loads(manifest_path.read_text("utf-8"))
        payload["stages"]["render"]["schema_version"] = 99
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "index schema 99" in cli_output(result)

    def test_a_missing_source_is_refused_before_any_encode(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        Path(run.manifest()["input"]["path"]).unlink()
        encodes = len(media.calls)

        result = render(run)

        assert result.exit_code == EXIT_INVALID_INPUT, cli_output(result)
        assert "source is missing" in cli_output(result)
        assert len(media.calls) == encodes


class TestPendingRollback:
    """A backup an earlier failure could not put back is resolved, or refused."""

    def strand(self, run: Analysed) -> Path:
        """Leave a pending backup in clips/, by failing the restore itself.

        The patches go through a scoped ``MonkeyPatch`` of their own rather than
        the test's fixture. The fixture is the same object the ``harness``
        used to set CONTENT_ENGINE_WORKSPACE, so calling ``undo()`` on it here
        would also unset the workspace and every later command would report the
        run as missing.
        """
        from content_engine.services import render_service

        assert render(run).exit_code == EXIT_SUCCESS
        clips = run.run_path.joinpath("clips")
        real_write = render_service.write_json
        real_replace = Path.replace

        def refuse_index(path: Path, value: Any) -> None:
            if path.name == "index.json":
                raise OSError("synthetic publication failure")
            real_write(path, value)

        def refuse_restore(self: Path, target: Any) -> Any:
            if ".rollback" in str(self) and self.suffix != ".tmp":
                raise OSError("synthetic restore failure")
            return real_replace(self, target)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(render_service, "write_json", refuse_index)
            patched.setattr(Path, "replace", refuse_restore)
            assert render(run, "--force").exit_code == EXIT_RENDER
        assert clips.joinpath(".rollback").is_dir()
        return clips

    def test_a_later_invocation_finishes_the_restore_and_says_so(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        clips = self.strand(run)

        result = render(run)

        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "Recovered" in cli_output(result)
        assert not clips.joinpath(".rollback").exists()

    def test_a_backup_that_cannot_be_resolved_fails_the_run_and_keeps_the_data(
        self, analysed: Analysed, media: FakeMedia
    ) -> None:
        run = reviewed(analysed)
        clips = self.strand(run)
        held = sorted(path.name for path in clips.joinpath(".rollback").iterdir())
        clips.joinpath(".rollback", "rollback.json").unlink()

        result = render(run)

        assert result.exit_code == EXIT_RENDER, cli_output(result)
        assert run.manifest()["status"] == RunStatus.FAILED_RENDER.value
        remaining = sorted(path.name for path in clips.joinpath(".rollback").iterdir())
        assert remaining == [name for name in held if name != "rollback.json"]
