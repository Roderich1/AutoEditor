"""The analyze command, end to end from a transcribed run.

Every test here goes through the real CLI against a real run directory: create,
transcribe with the fake transcriber, then analyze with a fixture. Nothing calls
FFmpeg, a model or a provider, and the run that comes out is the one a user
would have.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.analysis.fixture_analyzer import FixtureBatch
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import WORKSPACE_ENV_VAR, load_settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_ANALYSIS,
    EXIT_INVALID_INPUT,
    EXIT_SUCCESS,
)
from content_engine.services.analysis_service import (
    ARTIFACT_FILENAMES,
    CANDIDATES_FILENAME,
    RAW_CANDIDATES_FILENAME,
    STAGE_CONFIG_FILENAME,
)
from content_engine.services.chunking_service import CHUNKS_FILENAME
from content_engine.services.run_service import RunService
from tests.conftest import (
    FakeTranscriber,
    analysis_fixture,
    cli_output,
    fake_process,
    raw_candidate,
    write_fixture,
)

runner = CliRunner()
AUDIO_DURATION = 119.0


@dataclass
class Harness:
    run_id: str
    run_path: Path
    fixture_path: Path
    tmp_path: Path

    @property
    def analysis(self) -> Path:
        return self.run_path.joinpath("analysis")

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.run_path.joinpath("manifest.json").read_text(encoding="utf-8"))

    def candidates(self) -> dict[str, Any]:
        return json.loads(self.analysis.joinpath(CANDIDATES_FILENAME).read_text(encoding="utf-8"))

    def snapshot(self) -> dict[str, bytes]:
        return {
            name: self.analysis.joinpath(name).read_bytes()
            for name in ARTIFACT_FILENAMES
            if self.analysis.joinpath(name).is_file()
        } | {"manifest.json": self.run_path.joinpath("manifest.json").read_bytes()}


def _segments(count: int = 12) -> tuple:
    from tests.conftest import raw_segment, raw_word

    return tuple(
        raw_segment(
            index * 10.0,
            index * 10.0 + 9.0,
            f"segmento {index}",
            (
                raw_word("hola", index * 10.0, index * 10.0 + 0.5),
                raw_word("mundo", index * 10.0 + 8.5, index * 10.0 + 9.0),
            ),
        )
        for index in range(count)
    )


DEFAULT_BATCH = FixtureBatch(
    chunk_id="chunk_0000",
    raw_response='{"candidates": [{"start": 10.2, "end": 39.4}]}',
    candidates=[raw_candidate(10.2, 39.4), raw_candidate(60.0, 85.0, hook=70)],
)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    from tests.conftest import raw_transcription

    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("workspace")))
    monkeypatch.delenv("CONTENT_ENGINE_ANALYSIS_MODEL", raising=False)
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )

    def fake_probe(arguments: Sequence[str], timeout: float | None = None) -> Any:
        payload = {
            "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            "format": {"duration": str(AUDIO_DURATION), "format_name": "wav"},
        }
        return fake_process(arguments, json.dumps(payload))

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_probe)

    video = tmp_path.joinpath("sample.mp4")
    video.write_bytes(b"video")
    settings = load_settings()
    workspace = RunWorkspace(settings.workspace.root)
    service = RunService(settings, workspace)
    run_path, manifest = service.create(video)
    manifest = service.advance(run_path, manifest, RunStatus.INSPECTED)
    run_path.joinpath("audio", "source.wav").write_bytes(b"wav-bytes")
    service.advance(run_path, manifest, RunStatus.AUDIO_READY)

    transcriber = FakeTranscriber(raw_transcription(_segments(), AUDIO_DURATION))
    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: transcriber)
    assert runner.invoke(cli.app, ["transcribe", run_path.name]).exit_code == EXIT_SUCCESS

    fixture_path = write_fixture(
        tmp_path.joinpath("fixture.json"), analysis_fixture([DEFAULT_BATCH])
    )
    return Harness(run_path.name, run_path, fixture_path, tmp_path)


def _analyze(harness: Harness, *extra: str, fixture: Path | None = None) -> Any:
    return runner.invoke(
        cli.app,
        ["analyze", harness.run_id, "--fixture", str(fixture or harness.fixture_path), *extra],
    )


def _rewrite_fixture(harness: Harness, *batches: FixtureBatch) -> Path:
    return write_fixture(harness.fixture_path, analysis_fixture(list(batches)))


# --- the happy path ----------------------------------------------------------


def test_a_first_analysis_writes_every_artifact_and_reaches_analyzed(
    harness: Harness,
) -> None:
    result = _analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    for name in ARTIFACT_FILENAMES:
        assert harness.analysis.joinpath(name).is_file(), name
    assert harness.manifest()["status"] == RunStatus.ANALYZED


def test_the_manifest_records_the_stage_with_both_digests(harness: Harness) -> None:
    _analyze(harness)

    record = harness.manifest()["stages"][RunStage.ANALYSIS.value]
    assert len(record["fingerprint"]) == 64
    assert len(record["stage_config_sha256"]) == 64
    assert record["schema_version"] == 1
    assert record["completed_at"]


def test_the_manifest_names_the_fixture_rather_than_the_configured_provider(
    harness: Harness,
) -> None:
    """The run was analysed from a file. A manifest saying `gemini` would assert
    an external call that never happened."""
    _analyze(harness)

    versions = harness.manifest()["versions"]
    assert versions["analysis_provider"] == "fixture"
    assert versions["analysis_model"] == "fake-fixture-model"


def test_the_prompt_fields_ce_026_owns_stay_empty(harness: Harness) -> None:
    """clip_candidates/v1 does not exist. The fake's identity is recorded in the
    stage configuration, where it is true, and not in the field reserved for the
    real prompt."""
    _analyze(harness)

    versions = harness.manifest()["versions"]
    assert versions["prompt_version"] is None
    assert versions["prompt_sha256"] is None
    config = json.loads(
        harness.analysis.joinpath(STAGE_CONFIG_FILENAME).read_text(encoding="utf-8")
    )
    assert config["prompt_version"] == "fake-fixture/v1"


def test_the_output_reports_the_whole_funnel(harness: Harness) -> None:
    output = cli_output(_analyze(harness))

    assert "Candidates ready" in output
    assert "selected of" in output
    assert "fixture/fake-fixture-model" in output


def test_the_candidates_artifact_holds_a_ranked_shortlist(harness: Harness) -> None:
    _analyze(harness)

    payload = harness.candidates()
    assert [candidate["rank"] for candidate in payload["candidates"]] == [1, 2]
    assert payload["counts"]["proposed"] == 2
    assert payload["counts"]["selected"] == 2


def test_the_boundaries_were_snapped_onto_the_transcript(harness: Harness) -> None:
    """10.2 to 39.4 becomes 10.0 to 39.0: the segment edges either side of it."""
    _analyze(harness)

    first = harness.candidates()["candidates"][0]
    assert (first["start"], first["end"]) == (10.0, 39.0)
    assert first["boundary"]["proposed_start"] == 10.2


def test_a_transcript_with_no_speech_analyses_to_an_empty_shortlist(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import raw_transcription

    transcriber = FakeTranscriber(raw_transcription((), AUDIO_DURATION))
    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: transcriber)
    runner.invoke(cli.app, ["transcribe", harness.run_id, "--force"])
    _rewrite_fixture(harness)

    result = _analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert harness.candidates()["counts"]["proposed"] == 0
    assert "proposed no candidates" in cli_output(result)


def test_invalid_proposals_are_reported_and_kept(harness: Harness) -> None:
    _rewrite_fixture(
        harness,
        FixtureBatch(
            chunk_id="chunk_0000",
            raw_response="[]",
            candidates=[raw_candidate(10.2, 39.4), raw_candidate(-5.0, 30.0)],
        ),
    )

    result = _analyze(harness)

    assert "Refused 1 proposals" in cli_output(result)
    assert harness.candidates()["counts"]["invalid"] == 1
    assert len(harness.candidates()["invalid"]) == 1


# --- reuse -------------------------------------------------------------------


def test_a_second_analysis_reuses_the_candidates_and_writes_nothing(
    harness: Harness,
) -> None:
    _analyze(harness)
    before = harness.snapshot()

    result = _analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert "Candidates reused" in cli_output(result)
    assert harness.snapshot() == before


def test_reuse_leaves_every_byte_of_all_four_artifacts_untouched(
    harness: Harness,
) -> None:
    _analyze(harness)
    before = harness.snapshot()

    for _ in range(3):
        _analyze(harness)

    assert harness.snapshot() == before


# --- refusal -----------------------------------------------------------------


def _assert_refused(harness: Harness, result: Any, before: dict[str, bytes]) -> None:
    assert result.exit_code == EXIT_INVALID_INPUT
    assert "--force" in cli_output(result)
    assert harness.snapshot() == before


def test_a_changed_fixture_is_refused_without_force(harness: Harness) -> None:
    _analyze(harness)
    before = harness.snapshot()
    _rewrite_fixture(
        harness,
        FixtureBatch(
            chunk_id="chunk_0000", raw_response="[]", candidates=[raw_candidate(20.0, 45.0)]
        ),
    )

    _assert_refused(harness, _analyze(harness), before)


def test_a_changed_configuration_is_refused_without_force(harness: Harness, tmp_path: Path) -> None:
    _analyze(harness)
    before = harness.snapshot()
    profile = tmp_path.joinpath("profile.toml")
    profile.write_text("[analysis.candidates]\nmin_score = 10\n", encoding="utf-8")

    _assert_refused(harness, _analyze(harness, "--config", str(profile)), before)


def test_a_changed_transcript_is_refused_without_force(harness: Harness) -> None:
    _analyze(harness)
    before = harness.snapshot()
    path = harness.run_path.joinpath("transcript", "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["language_probability"] = 0.5
    path.write_text(json.dumps(payload), encoding="utf-8")

    _assert_refused(harness, _analyze(harness), before)


def test_an_edited_candidates_artifact_is_refused(harness: Harness) -> None:
    _analyze(harness)
    path = harness.analysis.joinpath(CANDIDATES_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["counts"]["selected"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = harness.snapshot()

    _assert_refused(harness, _analyze(harness), before)


def test_an_edited_stage_configuration_is_refused(harness: Harness) -> None:
    _analyze(harness)
    path = harness.analysis.joinpath(STAGE_CONFIG_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dedupe_iou"] = 0.99
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = harness.snapshot()

    _assert_refused(harness, _analyze(harness), before)


def test_a_deleted_raw_artifact_is_refused(harness: Harness) -> None:
    _analyze(harness)
    harness.analysis.joinpath(RAW_CANDIDATES_FILENAME).unlink()
    before = harness.snapshot()

    _assert_refused(harness, _analyze(harness), before)


def test_candidates_with_no_recorded_stage_are_refused(harness: Harness) -> None:
    _analyze(harness)
    path = harness.run_path.joinpath("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["stages"][RunStage.ANALYSIS.value]
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = harness.snapshot()

    _assert_refused(harness, _analyze(harness), before)


def test_candidates_recorded_under_another_schema_are_refused(harness: Harness) -> None:
    _analyze(harness)
    path = harness.run_path.joinpath("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stages"][RunStage.ANALYSIS.value]["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = harness.snapshot()

    _assert_refused(harness, _analyze(harness), before)


# --- force -------------------------------------------------------------------


def test_force_regenerates_the_artifacts(harness: Harness) -> None:
    _analyze(harness)
    _rewrite_fixture(
        harness,
        FixtureBatch(
            chunk_id="chunk_0000", raw_response="[]", candidates=[raw_candidate(20.0, 45.0)]
        ),
    )

    result = _analyze(harness, "--force")

    assert result.exit_code == EXIT_SUCCESS
    assert harness.candidates()["counts"]["proposed"] == 1
    assert harness.candidates()["candidates"][0]["start"] == 20.0


def test_force_records_a_new_fingerprint(harness: Harness) -> None:
    _analyze(harness)
    first = harness.manifest()["stages"][RunStage.ANALYSIS.value]["fingerprint"]
    _rewrite_fixture(
        harness,
        FixtureBatch(
            chunk_id="chunk_0000", raw_response="[]", candidates=[raw_candidate(20.0, 45.0)]
        ),
    )

    _analyze(harness, "--force")

    assert harness.manifest()["stages"][RunStage.ANALYSIS.value]["fingerprint"] != first


def test_force_warns_about_downstream_artifacts_it_invalidates(harness: Harness) -> None:
    _analyze(harness)
    harness.run_path.joinpath("review", "decisions.json").write_text("{}", encoding="utf-8")

    output = cli_output(_analyze(harness, "--force"))

    assert "review" in output
    assert "stale" in output
    assert "CE-052" in output


def test_a_failure_during_force_leaves_the_previous_artifacts_intact(
    harness: Harness,
) -> None:
    """Nothing is written until the whole stage has succeeded, so the run keeps
    the candidates it had rather than half of a newer set."""
    _analyze(harness)
    before = {name: harness.analysis.joinpath(name).read_bytes() for name in ARTIFACT_FILENAMES}
    _rewrite_fixture(harness, FixtureBatch(chunk_id="chunk_0000", error="provider exploded"))

    result = _analyze(harness, "--force")

    assert result.exit_code == EXIT_ANALYSIS
    assert {
        name: harness.analysis.joinpath(name).read_bytes() for name in ARTIFACT_FILENAMES
    } == before


# --- failures ----------------------------------------------------------------


def test_an_analyzer_failure_is_recorded_as_a_failed_analysis(harness: Harness) -> None:
    _rewrite_fixture(harness, FixtureBatch(chunk_id="chunk_0000", error="provider exploded"))

    result = _analyze(harness)

    assert result.exit_code == EXIT_ANALYSIS
    manifest = harness.manifest()
    assert manifest["status"] == RunStatus.FAILED_ANALYSIS
    assert manifest["failure"]["stage"] == RunStage.ANALYSIS.value
    assert "provider exploded" in manifest["failure"]["message"]


def test_a_failed_run_is_kept_for_diagnosis(harness: Harness) -> None:
    _rewrite_fixture(harness, FixtureBatch(chunk_id="chunk_0000", error="boom"))

    output = cli_output(_analyze(harness))

    assert "kept for diagnosis" in output
    assert harness.run_path.is_dir()


def test_a_missing_fixture_is_an_input_failure(harness: Harness) -> None:
    result = _analyze(harness, fixture=harness.tmp_path.joinpath("absent.json"))

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "does not exist" in cli_output(result)


def test_a_corrupt_fixture_is_an_input_failure_not_a_traceback(
    harness: Harness,
) -> None:
    harness.fixture_path.write_text("{not json", encoding="utf-8")

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Traceback" not in result.output


def test_a_fixture_from_another_schema_is_an_input_failure(harness: Harness) -> None:
    payload = json.loads(harness.fixture_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    harness.fixture_path.write_text(json.dumps(payload), encoding="utf-8")

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "declares fixture schema" in cli_output(result)


def test_a_fixture_that_does_not_cover_the_run_is_refused(harness: Harness) -> None:
    _rewrite_fixture(harness, FixtureBatch(chunk_id="chunk_0007", raw_response="[]"))
    before = harness.run_path.joinpath("manifest.json").read_bytes()

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "chunks this run does not have" in cli_output(result)
    assert harness.run_path.joinpath("manifest.json").read_bytes() == before


def test_a_run_without_a_transcript_is_refused(harness: Harness) -> None:
    harness.run_path.joinpath("transcript", "transcript.json").unlink()

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "no transcript" in cli_output(result)


def test_a_corrupt_transcript_is_refused(harness: Harness) -> None:
    harness.run_path.joinpath("transcript", "transcript.json").write_text(
        "{not json", encoding="utf-8"
    )

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "cannot be read as a transcript" in cli_output(result)


def test_a_transcript_from_another_schema_is_refused(harness: Harness) -> None:
    path = harness.run_path.joinpath("transcript", "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "declares transcript schema" in cli_output(result)


def test_an_unknown_run_is_refused(harness: Harness) -> None:
    result = runner.invoke(
        cli.app, ["analyze", "no-such-run", "--fixture", str(harness.fixture_path)]
    )

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "does not exist" in cli_output(result)


def test_a_run_that_has_not_been_transcribed_cannot_be_analyzed(
    harness: Harness,
) -> None:
    """Refused before anything is read, so the run is not half-processed by a
    command that was going to fail at the end anyway."""
    path = harness.run_path.joinpath("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = RunStatus.CREATED.value
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Cannot move a run" in cli_output(result)


# --- environment -------------------------------------------------------------


def test_analysis_works_from_a_directory_outside_the_repository(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace follows the environment, never the installation directory."""
    elsewhere = tmp_path.joinpath("un directorio con espacios y ñ")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = _analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert harness.analysis.joinpath(CANDIDATES_FILENAME).is_file()


def test_analysis_never_reads_the_provider_credential(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_getenv = os.getenv
    monkeypatch.setattr(
        os,
        "getenv",
        lambda name, default=None: (seen.append(name), real_getenv(name, default))[1],
    )

    assert _analyze(harness).exit_code == EXIT_SUCCESS
    assert "GEMINI_API_KEY" not in seen


def test_no_artifact_mentions_a_credential_name(harness: Harness) -> None:
    _analyze(harness)

    for name in ARTIFACT_FILENAMES:
        text = harness.analysis.joinpath(name).read_text(encoding="utf-8")
        assert "GEMINI_API_KEY" not in text
        assert "api_key" not in text.lower()


def test_every_artifact_is_written_with_lf_endings_and_no_bom(harness: Harness) -> None:
    _analyze(harness)

    for name in (*ARTIFACT_FILENAMES, "../manifest.json"):
        data = harness.analysis.joinpath(name).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), name
        assert b"\r\n" not in data, name


def test_no_temporary_file_is_left_in_the_run(harness: Harness) -> None:
    _analyze(harness)

    assert list(harness.run_path.rglob("*.tmp")) == []


def test_the_chunks_artifact_is_written_beside_the_candidates(harness: Harness) -> None:
    _analyze(harness)

    payload = json.loads(harness.analysis.joinpath(CHUNKS_FILENAME).read_text(encoding="utf-8"))
    assert payload["window_seconds"] == 360
    assert payload["overlap_seconds"] == 30
    assert len(payload["chunks"]) == 1
