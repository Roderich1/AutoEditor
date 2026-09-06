"""The analysis stage: what it writes, what it records, and what it refuses.

Everything here runs against the fixture analyzer, so nothing calls a provider,
and the assertions are mostly about artifacts: that all four are written, that
they describe the run that produced them, and that a later invocation can prove
they still do.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from content_engine.adapters.analysis.fixture_analyzer import (
    ANALYZER_VERSION,
    PROMPT_SHA256,
    PROMPT_VERSION,
    AnalysisFixture,
    FixtureAnalyzer,
    FixtureBatch,
    fixture_sha256,
)
from content_engine.config import Settings
from content_engine.domain.analysis_rules import (
    analysis_fingerprint,
    stage_config_sha256,
)
from content_engine.domain.candidates import (
    ANALYSIS_STAGE_CONFIG_SCHEMA_VERSION,
    CANDIDATES_SCHEMA_VERSION,
    RAW_CANDIDATES_SCHEMA_VERSION,
)
from content_engine.domain.exceptions import AnalysisError, IncompatibleArtifactError
from content_engine.ports.analyzer import AnalysisContext, CandidateBatch
from content_engine.services.analysis_service import (
    ARTIFACT_FILENAMES,
    CANDIDATES_FILENAME,
    RAW_CANDIDATES_FILENAME,
    STAGE_CONFIG_FILENAME,
    AnalysisService,
    plan_analysis,
    read_candidates,
    read_chunks,
    read_raw_collection,
    read_stage_config,
    verify_analysis,
)
from content_engine.services.chunking_service import CHUNKS_FILENAME, transcript_sha256
from tests.conftest import analysis_fixture, raw_candidate, speech_transcript, write_fixture

GENERATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _fixture(candidates: list | None = None, **overrides: object) -> AnalysisFixture:
    batch = FixtureBatch(
        chunk_id="chunk_0000",
        raw_response='{"candidates": [{"start": 10.0}]}',
        candidates=candidates if candidates is not None else [raw_candidate(10.2, 39.4)],
        **overrides,  # type: ignore[arg-type]
    )
    return analysis_fixture([batch])


def _run(
    settings: Settings,
    directory: Path,
    fixture: AnalysisFixture | None = None,
    transcript=None,
):
    analyzer = FixtureAnalyzer(fixture if fixture is not None else _fixture())
    plan = plan_analysis(transcript or speech_transcript(), settings, analyzer.identity)
    outcome = AnalysisService(analyzer, analyzer.identity).analyze(plan, directory, GENERATED_AT)
    return analyzer, plan, outcome


# --- artifacts ---------------------------------------------------------------


def test_all_four_artifacts_are_written(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    for name in ARTIFACT_FILENAMES:
        assert directory.joinpath(name).is_file(), name


def test_no_temporary_file_survives(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    assert list(directory.glob("*.tmp")) == []


def test_every_artifact_is_utf8_without_a_bom_and_with_lf_endings(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    for name in ARTIFACT_FILENAMES:
        data = directory.joinpath(name).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), name
        assert b"\r\n" not in data, name
        data.decode("utf-8")


def test_no_artifact_contains_a_non_finite_number(settings: Settings, tmp_path: Path) -> None:
    """NaN and the infinities are Python extensions no conforming parser reads."""
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    for name in ARTIFACT_FILENAMES:
        text = directory.joinpath(name).read_text(encoding="utf-8")
        assert "NaN" not in text
        assert "Infinity" not in text
        json.loads(text, parse_constant=_refuse_constant)


def _refuse_constant(value: str) -> float:  # pragma: no cover - guard for the test above
    raise AssertionError(f"artifact contains {value}")


def test_the_raw_artifact_preserves_the_response_byte_for_byte(
    settings: Settings, tmp_path: Path
) -> None:
    odd = '  {"a": 1}\r\n\ttrailing  '
    directory = tmp_path.joinpath("analysis")
    fixture = analysis_fixture(
        [FixtureBatch(chunk_id="chunk_0000", raw_response=odd, candidates=[])]
    )

    _run(settings, directory, fixture)

    stored = read_raw_collection(directory)
    assert stored.batches[0].raw_response == odd


def test_the_raw_artifact_keeps_every_proposal_including_the_refused_ones(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")
    fixture = _fixture([raw_candidate(10.0, 39.0), raw_candidate(-5.0, 30.0)])

    _, _, outcome = _run(settings, directory, fixture)

    assert outcome.raw.proposed_count == 2
    assert len(read_raw_collection(directory).batches[0].candidates) == 2
    assert outcome.collection.counts.invalid == 1


def test_the_declared_schema_versions_are_the_ones_this_build_produces(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    assert read_raw_collection(directory).schema_version == RAW_CANDIDATES_SCHEMA_VERSION
    assert read_candidates(directory).schema_version == CANDIDATES_SCHEMA_VERSION
    assert read_stage_config(directory).schema_version == ANALYSIS_STAGE_CONFIG_SCHEMA_VERSION


def test_the_chunks_artifact_names_the_transcript_it_was_cut_from(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()

    _run(settings, directory, transcript=transcript)

    payload = json.loads(directory.joinpath(CHUNKS_FILENAME).read_text(encoding="utf-8"))
    assert payload["transcript_sha256"] == transcript_sha256(transcript)


# --- the stage configuration -------------------------------------------------


def test_the_stage_configuration_names_the_analyzer_that_actually_ran(
    settings: Settings, tmp_path: Path
) -> None:
    """The configuration says Gemini. Nothing called Gemini. The artifact has to
    say which of those two facts describes this run."""
    directory = tmp_path.joinpath("analysis")

    fixture = analysis_fixture(
        [FixtureBatch(chunk_id="chunk_0000", raw_response="[]")], model="fake-model-7"
    )

    _, _, outcome = _run(settings, directory, fixture)

    config = outcome.stage_config
    assert config.analyzer == "fixture"
    assert config.analyzer_version == ANALYZER_VERSION
    assert config.model == "fake-model-7"
    assert config.provider_configured == "gemini"
    assert config.model_configured == settings.analysis.model


def test_the_stage_configuration_records_the_prompt_that_was_used(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _, _, outcome = _run(settings, directory)

    assert outcome.stage_config.prompt_version == PROMPT_VERSION
    assert outcome.stage_config.prompt_sha256 == PROMPT_SHA256


def test_the_stage_configuration_records_every_rule_version(
    settings: Settings, tmp_path: Path
) -> None:
    """A change to snapping or deduplication alters the shortlist without
    altering a single setting, so a run that could not name its rules would be
    unreproducible in the way that is hardest to notice."""
    directory = tmp_path.joinpath("analysis")

    _, _, outcome = _run(settings, directory)

    payload = outcome.stage_config.model_dump(mode="json")
    for field in (
        "chunking_rules_version",
        "validation_rules_version",
        "boundary_rules_version",
        "dedupe_rules_version",
        "ranking_rules_version",
        "candidate_id_version",
        "score_formula_version",
    ):
        assert isinstance(payload[field], int), field


def test_the_stage_configuration_carries_no_credential_or_machine_path(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _run(settings, directory)

    text = directory.joinpath(STAGE_CONFIG_FILENAME).read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" not in text
    assert "api_key" not in text.lower()
    assert str(tmp_path) not in text


def test_the_stage_configuration_digest_covers_the_file_as_written(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _, _, outcome = _run(settings, directory)

    assert stage_config_sha256(read_stage_config(directory)) == outcome.stage_config_sha256


# --- the fingerprint ---------------------------------------------------------


def test_the_fingerprint_rebuilds_from_what_is_on_disk(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()

    _, _, outcome = _run(settings, directory, transcript=transcript)

    rebuilt = analysis_fingerprint(
        transcript_sha256(transcript),
        read_chunks(directory),
        read_raw_collection(directory),
        read_candidates(directory),
        read_stage_config(directory),
    )
    assert rebuilt == outcome.fingerprint


def test_the_same_inputs_produce_the_same_fingerprint(settings: Settings, tmp_path: Path) -> None:
    """Same inputs and the same generated_at. The fingerprint now covers the
    artifacts, so the timestamp inside candidates.json is part of it: two runs
    of identical inputs at different moments differ, deliberately. It identifies
    one execution's files, not a portable experiment."""
    first = _run(settings, tmp_path.joinpath("a"))[2]
    second = _run(settings, tmp_path.joinpath("b"))[2]

    assert first.fingerprint == second.fingerprint
    assert first.stage_config_sha256 == second.stage_config_sha256


def test_the_fingerprint_covers_the_moment_the_collection_was_generated(
    settings: Settings, tmp_path: Path
) -> None:
    analyzer = FixtureAnalyzer(_fixture())
    plan = plan_analysis(speech_transcript(), settings, analyzer.identity)
    service = AnalysisService(analyzer, analyzer.identity)

    first = service.analyze(plan, tmp_path.joinpath("a"), GENERATED_AT)
    second = service.analyze(plan, tmp_path.joinpath("b"), datetime(2026, 6, 1, tzinfo=UTC))

    assert first.fingerprint != second.fingerprint


def test_a_different_fixture_produces_a_different_fingerprint(
    settings: Settings, tmp_path: Path
) -> None:
    first = _run(settings, tmp_path.joinpath("a"))[2]
    second = _run(settings, tmp_path.joinpath("b"), _fixture([raw_candidate(10.0, 40.0)]))[2]

    assert first.fingerprint != second.fingerprint


def test_a_different_transcript_produces_a_different_fingerprint(
    settings: Settings, tmp_path: Path
) -> None:
    first = _run(settings, tmp_path.joinpath("a"))[2]
    second = _run(settings, tmp_path.joinpath("b"), transcript=speech_transcript(count=11))[2]

    assert first.fingerprint != second.fingerprint


def test_the_fingerprint_is_a_lowercase_hex_digest(settings: Settings, tmp_path: Path) -> None:
    _, _, outcome = _run(settings, tmp_path.joinpath("analysis"))

    assert len(outcome.fingerprint) == 64
    assert outcome.fingerprint == outcome.fingerprint.lower()


# --- degenerate inputs -------------------------------------------------------


def test_a_transcript_with_no_speech_produces_no_chunks_and_no_candidates(
    settings: Settings, tmp_path: Path
) -> None:
    """Audio with nothing recognisable in it is a real input, so it ends with an
    empty shortlist rather than an exception."""
    directory = tmp_path.joinpath("analysis")
    silent = speech_transcript().model_copy(update={"segments": []})

    _, plan, outcome = _run(settings, directory, analysis_fixture([]), transcript=silent)

    assert plan.chunks.chunks == []
    assert outcome.collection.counts.proposed == 0
    assert outcome.collection.candidates == []
    for name in ARTIFACT_FILENAMES:
        assert directory.joinpath(name).is_file()


def test_a_chunk_answered_with_nothing_still_produces_a_batch(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")

    _, _, outcome = _run(settings, directory, _fixture([]))

    assert len(outcome.raw.batches) == 1
    assert outcome.raw.proposed_count == 0


# --- failures ----------------------------------------------------------------


def test_an_analyzer_failure_leaves_no_artifact_behind(settings: Settings, tmp_path: Path) -> None:
    """Nothing is written until everything is valid, so a failed stage cannot
    leave a directory a later run would mistake for a completed one."""
    directory = tmp_path.joinpath("analysis")
    fixture = analysis_fixture([FixtureBatch(chunk_id="chunk_0000", error="boom")])

    with pytest.raises(AnalysisError, match="boom"):
        _run(settings, directory, fixture)

    assert not directory.exists() or list(directory.iterdir()) == []


def test_an_analyzer_answering_about_another_chunk_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """Candidates attached to the wrong material would be validated against a
    window they did not come from."""

    class Confused:
        def find_candidates(self, chunk, context: AnalysisContext) -> CandidateBatch:
            return CandidateBatch(chunk_id="chunk_9999", candidates=(), raw_response="", model="m")

    analyzer = FixtureAnalyzer(_fixture())
    plan = plan_analysis(speech_transcript(), settings, analyzer.identity)
    service = AnalysisService(Confused(), analyzer.identity)
    directory = tmp_path.joinpath("analysis")

    with pytest.raises(AnalysisError, match="answered about chunk_9999"):
        service.analyze(plan, directory, GENERATED_AT)


# --- verification ------------------------------------------------------------


def _verify(settings: Settings, directory: Path, outcome, plan, transcript) -> None:
    verify_analysis(
        directory,
        outcome.fingerprint,
        outcome.stage_config_sha256,
        transcript_sha256(transcript),
        plan,
    )


def test_unchanged_artifacts_verify(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    _, plan, outcome = _run(settings, directory, transcript=transcript)

    collection = verify_analysis(
        directory,
        outcome.fingerprint,
        outcome.stage_config_sha256,
        transcript_sha256(transcript),
        plan,
    )

    assert collection == outcome.collection


@pytest.mark.parametrize(
    "name", [CHUNKS_FILENAME, RAW_CANDIDATES_FILENAME, STAGE_CONFIG_FILENAME, CANDIDATES_FILENAME]
)
def test_a_deleted_artifact_is_refused(settings: Settings, tmp_path: Path, name: str) -> None:
    """All four, with no exception. chunks.json used to be excused because it
    could be rebuilt from the transcript; a file trusted for that reason is a
    file nobody checked, and the recorded answers beside it are answers to
    whatever it actually said."""
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    _, plan, outcome = _run(settings, directory, transcript=transcript)
    directory.joinpath(name).unlink()

    with pytest.raises(IncompatibleArtifactError):
        _verify(settings, directory, outcome, plan, transcript)


def test_an_edited_stage_configuration_is_refused(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    _, plan, outcome = _run(settings, directory, transcript=transcript)
    path = directory.joinpath(STAGE_CONFIG_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["min_score"] = 1.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="does not match the manifest"):
        _verify(settings, directory, outcome, plan, transcript)


def test_an_edited_raw_response_is_refused(settings: Settings, tmp_path: Path) -> None:
    """The batches are in the fingerprint, so tampering with the evidence of what
    the provider said breaks the identity of the whole stage."""
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    _, plan, outcome = _run(settings, directory, transcript=transcript)
    path = directory.joinpath(RAW_CANDIDATES_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["batches"][0]["raw_response"] = "something else entirely"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="cannot be rebuilt"):
        _verify(settings, directory, outcome, plan, transcript)


def test_a_different_transcript_is_refused(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")
    _, plan, outcome = _run(settings, directory)
    other = transcript_sha256(speech_transcript(count=11))

    with pytest.raises(IncompatibleArtifactError, match="but the run holds"):
        verify_analysis(directory, outcome.fingerprint, outcome.stage_config_sha256, other, plan)


def test_a_different_fixture_is_refused(settings: Settings, tmp_path: Path) -> None:
    """The stored artifacts are internally consistent; what changed is what is
    being asked for now, and only comparing the two can see it."""
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    _, _, outcome = _run(settings, directory, transcript=transcript)

    other = FixtureAnalyzer(_fixture([raw_candidate(10.0, 40.0)]))
    other_plan = plan_analysis(transcript, settings, other.identity)
    digest = transcript_sha256(transcript)

    with pytest.raises(IncompatibleArtifactError, match="different settings"):
        verify_analysis(
            directory, outcome.fingerprint, outcome.stage_config_sha256, digest, other_plan
        )


def test_a_different_candidate_policy_is_refused(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    analyzer, _, outcome = _run(settings, directory, transcript=transcript)

    settings.analysis.candidates.min_score = 10.0
    other_plan = plan_analysis(transcript, settings, analyzer.identity)
    digest = transcript_sha256(transcript)

    with pytest.raises(IncompatibleArtifactError, match="different settings"):
        verify_analysis(
            directory, outcome.fingerprint, outcome.stage_config_sha256, digest, other_plan
        )


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        (RAW_CANDIDATES_FILENAME, read_raw_collection),
        (CANDIDATES_FILENAME, read_candidates),
        (STAGE_CONFIG_FILENAME, read_stage_config),
    ],
)
def test_an_artifact_from_another_schema_is_refused(
    settings: Settings, tmp_path: Path, name: str, reader
) -> None:
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    path = directory.joinpath(name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="schema"):
        reader(directory)


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        (RAW_CANDIDATES_FILENAME, read_raw_collection),
        (CANDIDATES_FILENAME, read_candidates),
        (STAGE_CONFIG_FILENAME, read_stage_config),
    ],
)
def test_an_artifact_that_is_not_json_is_refused(
    settings: Settings, tmp_path: Path, name: str, reader
) -> None:
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    directory.joinpath(name).write_text("{not json", encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="cannot be read"):
        reader(directory)


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        (RAW_CANDIDATES_FILENAME, read_raw_collection),
        (CANDIDATES_FILENAME, read_candidates),
        (STAGE_CONFIG_FILENAME, read_stage_config),
    ],
)
def test_an_artifact_that_is_not_an_object_is_refused(
    settings: Settings, tmp_path: Path, name: str, reader
) -> None:
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    directory.joinpath(name).write_text("[]", encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="does not contain"):
        reader(directory)


def test_an_invalid_candidate_collection_is_refused(settings: Settings, tmp_path: Path) -> None:
    """Reading it back re-runs every invariant, so an edited funnel is caught."""
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    path = directory.joinpath(CANDIDATES_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["counts"]["selected"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="not a valid candidate collection"):
        read_candidates(directory)


def test_a_raw_collection_whose_count_disagrees_with_its_batches_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    path = directory.joinpath(RAW_CANDIDATES_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["proposed_count"] = 42
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="not a valid raw candidate collection"):
        read_raw_collection(directory)


def test_the_fixture_digest_is_recorded_in_both_artifacts(
    settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path.joinpath("analysis")
    fixture = _fixture()

    _run(settings, directory, fixture)

    digest = fixture_sha256(fixture)
    assert read_stage_config(directory).fixture_sha256 == digest
    assert read_raw_collection(directory).fixture_sha256 == digest


def test_writing_a_fixture_to_disk_and_back_is_lossless(tmp_path: Path) -> None:
    from content_engine.adapters.analysis.fixture_analyzer import load_fixture

    fixture = _fixture()
    path = write_fixture(tmp_path.joinpath("f.json"), fixture)

    assert load_fixture(path) == fixture


def test_a_raw_collection_answering_one_chunk_twice_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """Two batches for one chunk would double-count every proposal in it."""
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    path = directory.joinpath(RAW_CANDIDATES_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["batches"] = [payload["batches"][0], payload["batches"][0]]
    payload["proposed_count"] = 2 * payload["proposed_count"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="same chunk"):
        read_raw_collection(directory)


def test_a_stage_configuration_with_an_impossible_value_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """The schema version can be right while the content is not."""
    directory = tmp_path.joinpath("analysis")
    _run(settings, directory)
    path = directory.joinpath(STAGE_CONFIG_FILENAME)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dedupe_iou"] = 7.5
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncompatibleArtifactError, match="not a valid analysis stage"):
        read_stage_config(directory)
