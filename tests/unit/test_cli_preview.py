"""CE-034 at the command boundary: `content-engine preview RUN_ID`.

The command owns three questions the service does not: whether this run may be
previewed at all, what the manifest is allowed to claim afterwards, and what a
second invocation over finished previews means.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from content_engine import cli
from content_engine.config import Settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_CONFIGURATION,
    EXIT_INVALID_INPUT,
    EXIT_RENDER,
    EXIT_SUCCESS,
)
from content_engine.domain.preview_rules import (
    PREVIEW_INDEX_FILENAME,
    PREVIEW_STAGE_CONFIG_FILENAME,
    preview_filename,
)
from content_engine.domain.previews import PREVIEW_INDEX_SCHEMA_VERSION
from tests.conftest import (
    Analysed,
    FakeMedia,
    Harness,
    analyse,
    cli_output,
    write_fixture,
)

runner = CliRunner()


def preview(run_id: str, *arguments: str) -> Result:
    return runner.invoke(cli.app, ["preview", run_id, *arguments])


def profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path.joinpath("profile.toml")
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


class TestGuards:
    def test_an_unknown_run_is_refused(self, media: FakeMedia, settings: Settings) -> None:
        result = preview("nope")
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "Run not found" in cli_output(result)

    def test_a_run_that_has_not_been_analysed_is_refused(
        self, harness: Harness, media: FakeMedia
    ) -> None:
        result = preview(harness.run_id)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert media.calls == []
        assert harness.manifest()["status"] == RunStatus.TRANSCRIBED

    def test_a_traversing_run_identifier_is_refused(self, media: FakeMedia) -> None:
        result = preview("../escape")
        assert result.exit_code == EXIT_INVALID_INPUT

    def test_previews_disabled_refuses_rather_than_advancing(
        self, analysed: Analysed, tmp_path: Path
    ) -> None:
        config = profile(tmp_path, "[preview]\nenabled = false\n")
        result = preview(analysed.run_id, "--config", str(config))
        assert result.exit_code == EXIT_CONFIGURATION
        output = cli_output(result)
        assert "preview.enabled" in output
        assert analysed.manifest()["status"] == RunStatus.ANALYZED
        assert RunStage.PREVIEW.value not in analysed.manifest()["stages"]
        assert analysed.media.calls == []

    def test_a_missing_source_is_refused_before_the_stage_starts(self, analysed: Analysed) -> None:
        """The environment is wrong, so nothing was attempted and nothing failed."""
        Path(analysed.manifest()["input"]["path"]).unlink()
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert analysed.manifest()["status"] == RunStatus.ANALYZED
        assert analysed.media.calls == []

    def test_a_replaced_source_is_refused(self, analysed: Analysed) -> None:
        Path(analysed.manifest()["input"]["path"]).write_bytes(b"another video entirely")
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "source" in cli_output(result)
        assert analysed.manifest()["status"] == RunStatus.ANALYZED
        assert analysed.media.calls == []


class TestSuccess:
    def test_it_produces_one_preview_per_selected_candidate(self, analysed: Analysed) -> None:
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        expected = {preview_filename(entry["id"]) for entry in analysed.candidates()}
        produced = {path.name for path in analysed.previews.glob("*.mp4")}
        assert produced == expected

    def test_the_run_reaches_ready_for_review(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        manifest = analysed.manifest()
        assert manifest["status"] == RunStatus.READY_FOR_REVIEW
        assert manifest["failure"] is None

    def test_the_stage_is_recorded_with_its_own_fingerprint(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        record = analysed.manifest()["stages"][RunStage.PREVIEW.value]
        assert len(record["fingerprint"]) == 64
        assert len(record["stage_config_sha256"]) == 64
        assert record["schema_version"] == PREVIEW_INDEX_SCHEMA_VERSION

    def test_the_analysis_stage_record_is_untouched(self, analysed: Analysed) -> None:
        before = analysed.manifest()["stages"]["analysis"]
        preview(analysed.run_id)
        assert analysed.manifest()["stages"]["analysis"] == before

    def test_the_index_describes_every_preview(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        index = json.loads(
            analysed.previews.joinpath(PREVIEW_INDEX_FILENAME).read_text(encoding="utf-8")
        )
        assert index["analysis_fingerprint"] == analysed.analysis_fingerprint()
        assert index["source_sha256"] == analysed.manifest()["input"]["sha256"]
        assert (index["width"], index["height"]) == (540, 960)
        for entry, candidate in zip(index["previews"], analysed.candidates(), strict=True):
            assert entry["candidate_id"] == candidate["id"]
            assert entry["rank"] == candidate["rank"]
            assert entry["start"] == candidate["start"]
            assert entry["end"] == candidate["end"]
            assert entry["filename"] == preview_filename(candidate["id"])
            assert len(entry["sha256"]) == 64

    def test_the_effective_configuration_is_written(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        assert analysed.previews.joinpath(PREVIEW_STAGE_CONFIG_FILENAME).is_file()

    def test_it_reports_what_it_produced(self, analysed: Analysed) -> None:
        result = preview(analysed.run_id)
        assert "2 previews" in cli_output(result)

    def test_no_temporary_file_is_left_anywhere_in_the_run(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        assert list(analysed.run_path.rglob("*.tmp")) == []
        assert [path for path in analysed.previews.iterdir() if path.is_dir()] == []


class TestZeroCandidates:
    @pytest.fixture
    def nothing_selected(self, harness: Harness, media: FakeMedia) -> Analysed:
        from content_engine.adapters.analysis.fixture_analyzer import FixtureBatch
        from tests.conftest import analysis_fixture, weak_candidate

        write_fixture(
            harness.fixture_path,
            analysis_fixture(
                [
                    FixtureBatch(
                        chunk_id="chunk_0000",
                        raw_response='{"candidates": []}',
                        candidates=[weak_candidate(10.0, 39.0)],
                    )
                ]
            ),
        )
        result = analyse(harness)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        return Analysed(harness, media)

    def test_it_succeeds_and_says_there_was_nothing_to_preview(
        self, nothing_selected: Analysed
    ) -> None:
        assert nothing_selected.candidates() == []
        result = preview(nothing_selected.run_id)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "no candidates" in cli_output(result).lower()

    def test_the_run_still_reaches_ready_for_review(self, nothing_selected: Analysed) -> None:
        preview(nothing_selected.run_id)
        assert nothing_selected.manifest()["status"] == RunStatus.READY_FOR_REVIEW

    def test_no_encoder_runs(self, nothing_selected: Analysed) -> None:
        preview(nothing_selected.run_id)
        assert nothing_selected.media.calls == []
        assert list(nothing_selected.previews.glob("*.mp4")) == []


class TestReuse:
    def test_a_second_invocation_reuses_without_encoding(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        encodes = len(analysed.media.calls)
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert "reused" in cli_output(result).lower()
        assert len(analysed.media.calls) == encodes

    def test_reuse_is_byte_identical(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        before = analysed.preview_snapshot()
        manifest_before = analysed.run_path.joinpath("manifest.json").read_bytes()
        preview(analysed.run_id)
        assert analysed.preview_snapshot() == before
        assert analysed.run_path.joinpath("manifest.json").read_bytes() == manifest_before

    def test_a_deleted_preview_prevents_reuse(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        victim = next(iter(analysed.previews.glob("*.mp4")))
        victim.unlink()
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--force" in cli_output(result)

    def test_a_truncated_preview_prevents_reuse(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        victim = next(iter(analysed.previews.glob("*.mp4")))
        victim.write_bytes(victim.read_bytes()[:2])
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_INVALID_INPUT

    def test_a_refusal_leaves_every_artifact_alone(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        analysed.previews.joinpath(PREVIEW_INDEX_FILENAME).unlink()
        before = analysed.preview_snapshot()
        preview(analysed.run_id)
        assert analysed.preview_snapshot() == before

    def test_changed_dimensions_prevent_reuse(self, analysed: Analysed, tmp_path: Path) -> None:
        preview(analysed.run_id)
        config = profile(tmp_path, "[preview]\nenabled = true\nwidth = 360\nheight = 640\n")
        result = preview(analysed.run_id, "--config", str(config))
        assert result.exit_code == EXIT_INVALID_INPUT
        assert "--force" in cli_output(result)


class TestForce:
    def test_it_regenerates_every_preview(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        encodes = len(analysed.media.calls)
        result = preview(analysed.run_id, "--force")
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert len(analysed.media.calls) == encodes * 2

    def test_a_failed_force_keeps_the_previous_previews(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        before = analysed.preview_snapshot()
        analysed.media.fail_for = {
            preview_filename(analysed.candidates()[-1]["id"]),
        }
        result = preview(analysed.run_id, "--force")
        assert result.exit_code == EXIT_RENDER
        assert analysed.preview_snapshot() == before

    def test_a_failed_force_does_not_leave_the_run_ready_for_review(
        self, analysed: Analysed
    ) -> None:
        preview(analysed.run_id)
        analysed.media.fail_for = {preview_filename(analysed.candidates()[-1]["id"])}
        preview(analysed.run_id, "--force")
        assert analysed.manifest()["status"] == RunStatus.FAILED_PREVIEW

    def test_a_stale_preview_of_a_dropped_candidate_is_removed(self, analysed: Analysed) -> None:
        preview(analysed.run_id)
        stray = analysed.previews.joinpath("candidate_cand_gone.mp4")
        stray.write_bytes(b"left over from an older shortlist")
        preview(analysed.run_id, "--force")
        assert not stray.exists()


class TestRecovery:
    def test_a_failed_run_whose_previews_are_intact_recovers(self, analysed: Analysed) -> None:
        """A later attempt failed without replacing the previews it left behind.

        Verification proves the existing set still describes this analysis, this
        source and these settings, so the stage is complete and the recorded
        failure no longer describes anything.
        """
        preview(analysed.run_id)
        analysed.media.fail_for = {preview_filename(analysed.candidates()[-1]["id"])}
        assert preview(analysed.run_id, "--force").exit_code == EXIT_RENDER
        assert analysed.manifest()["status"] == RunStatus.FAILED_PREVIEW

        analysed.media.fail_for = set()
        result = preview(analysed.run_id)
        assert result.exit_code == EXIT_SUCCESS, cli_output(result)
        assert analysed.manifest()["status"] == RunStatus.READY_FOR_REVIEW
        assert "recovered" in cli_output(result).lower()
