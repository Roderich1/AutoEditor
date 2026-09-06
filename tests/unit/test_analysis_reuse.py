"""Reuse must protect all four analysis artifacts, and they must agree.

The stage writes four files and records two digests. A reuse decision that only
proves two of the files are intact is not a reuse decision: whatever the other
two say is trusted without evidence, and everything downstream reads them.

Every test here edits exactly one thing on disk, keeping every artifact
internally valid, and asserts the refusal. An edit that broke a model's own
invariants would be caught by the reader and would prove nothing about the
fingerprint.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from content_engine.adapters.analysis.fixture_analyzer import (
    AnalysisFixture,
    FixtureAnalyzer,
    FixtureBatch,
)
from content_engine.config import Settings
from content_engine.domain.analysis_rules import coherence_problem
from content_engine.domain.candidates import (
    RawCandidateBatch,
    RawCandidateCollection,
)
from content_engine.domain.exceptions import AnalysisError, IncompatibleArtifactError
from content_engine.ports.analyzer import AnalysisContext, CandidateBatch
from content_engine.services.analysis_service import (
    ARTIFACT_FILENAMES,
    CANDIDATES_FILENAME,
    RAW_CANDIDATES_FILENAME,
    AnalysisService,
    plan_analysis,
    verify_analysis,
)
from content_engine.services.chunking_service import CHUNKS_FILENAME, transcript_sha256
from tests.conftest import analysis_fixture, raw_candidate, speech_transcript

GENERATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

#: Two well-formed, non-overlapping candidates on the ten-second grid, so the
#: collection has two ranked entries and no deduplication events. That leaves
#: room to edit a rank, an interval or a score without breaking any invariant.
BATCH = FixtureBatch(
    chunk_id="chunk_0000",
    raw_response='{"candidates": [{"start": 10.2, "end": 39.4}]}',
    candidates=[raw_candidate(10.2, 39.4), raw_candidate(60.0, 85.0, hook=70)],
)


def _fixture(batches: list[FixtureBatch] | None = None) -> AnalysisFixture:
    return analysis_fixture(batches if batches is not None else [BATCH])


def _run(settings: Settings, directory: Path, transcript=None, fixture=None):
    analyzer = FixtureAnalyzer(fixture if fixture is not None else _fixture())
    plan = plan_analysis(transcript or speech_transcript(), settings, analyzer.identity)
    outcome = AnalysisService(analyzer, analyzer.identity).analyze(plan, directory, GENERATED_AT)
    return plan, outcome


def _verify(directory: Path, outcome, plan, transcript):
    """Adapter over the verification entry point, so the tests read the same
    before and after the contract is widened to cover all four artifacts."""
    return verify_analysis(
        directory,
        outcome.fingerprint,
        outcome.stage_config_sha256,
        transcript_sha256(transcript),
        plan,
    )


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        name: directory.joinpath(name).read_bytes()
        for name in ARTIFACT_FILENAMES
        if directory.joinpath(name).is_file()
    }


def _edit(path: Path, mutate) -> None:
    """Rewrite one artifact through a mutation of its parsed payload."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


@pytest.fixture
def stage(settings: Settings, tmp_path: Path):
    """One completed analysis, plus everything needed to verify it again."""
    directory = tmp_path.joinpath("analysis")
    transcript = speech_transcript()
    plan, outcome = _run(settings, directory, transcript)
    return directory, outcome, plan, transcript


def _assert_refused(stage, match: str | None = None) -> None:
    directory, outcome, plan, transcript = stage
    before = _snapshot(directory)

    with pytest.raises(IncompatibleArtifactError, match=match):
        _verify(directory, outcome, plan, transcript)

    assert _snapshot(directory) == before, "a refusal wrote to disk"


# --- the four artifacts ------------------------------------------------------


def test_an_untouched_stage_verifies(stage) -> None:
    directory, outcome, plan, transcript = stage

    collection = _verify(directory, outcome, plan, transcript)

    assert collection == outcome.collection


@pytest.mark.parametrize("name", ARTIFACT_FILENAMES)
def test_a_missing_artifact_is_refused(stage, name: str) -> None:
    """All four, with no exception for the one that could be rebuilt. A file
    that is trusted because it could be regenerated is a file nobody checked."""
    directory = stage[0]
    directory.joinpath(name).unlink()

    _assert_refused(stage)


def test_an_edited_topic_in_the_candidates_is_refused(stage) -> None:
    """Free text, so the collection stays perfectly valid. Nothing but a digest
    over the file itself can see this."""
    directory = stage[0]
    _edit(
        directory.joinpath(CANDIDATES_FILENAME),
        lambda payload: payload["candidates"][0].update({"topic": "otro tema"}),
    )

    _assert_refused(stage)


def test_an_edited_interval_in_the_candidates_is_refused(stage) -> None:
    """Shifted by a second, boundary and deltas kept consistent, so every
    invariant of CandidateCollection still holds and the clip is a different
    clip."""

    def shift(payload: dict) -> None:
        candidate = payload["candidates"][0]
        candidate["start"] += 1.0
        candidate["end"] += 1.0
        boundary = candidate["boundary"]
        boundary["proposed_start"] += 1.0
        boundary["proposed_end"] += 1.0
        boundary["adjusted_start"] += 1.0
        boundary["adjusted_end"] += 1.0

    directory = stage[0]
    _edit(directory.joinpath(CANDIDATES_FILENAME), shift)

    _assert_refused(stage)


def test_an_edited_total_score_in_the_candidates_is_refused(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(CANDIDATES_FILENAME),
        lambda payload: payload["candidates"][0].update({"total_score": 12.5}),
    )

    _assert_refused(stage)


def test_a_reordered_ranking_is_refused(stage) -> None:
    """Ranks stay contiguous from 1 and every record stays valid; the shortlist
    a human would be shown is simply the other way round."""

    def swap(payload: dict) -> None:
        first, second = payload["candidates"]
        first["rank"], second["rank"] = second["rank"], first["rank"]
        payload["candidates"] = [second, first]

    directory = stage[0]
    _edit(directory.joinpath(CANDIDATES_FILENAME), swap)

    _assert_refused(stage)


def test_an_edited_chunk_text_is_refused(stage) -> None:
    """The chunk text is what the analyzer was shown. Changing it changes the
    question the recorded answers were answers to."""
    directory = stage[0]
    _edit(
        directory.joinpath(CHUNKS_FILENAME),
        lambda payload: payload["chunks"][0].update({"text": "otro texto"}),
    )

    _assert_refused(stage)


def test_edited_chunk_windowing_is_refused(stage) -> None:
    directory = stage[0]
    _edit(directory.joinpath(CHUNKS_FILENAME), lambda payload: payload.update({"rules_version": 9}))

    _assert_refused(stage)


def test_edited_raw_metadata_is_refused(stage) -> None:
    """Above the batches: the analyzer version that produced them. A digest over
    the batches alone cannot see this, and it is exactly the field that says
    which build's rules the answers were interpreted under."""
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload.update({"analyzer_version": "99"}),
    )

    _assert_refused(stage)


def test_an_edited_raw_prompt_identity_is_refused(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload.update({"prompt_sha256": "b" * 64}),
    )

    _assert_refused(stage)


def test_an_edited_raw_response_is_refused(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload["batches"][0].update({"raw_response": "otra respuesta"}),
    )

    _assert_refused(stage)


# --- the artifacts must agree with one another -------------------------------


def test_the_raw_collection_and_the_chunks_must_name_one_transcript(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload.update({"transcript_sha256": "c" * 64}),
    )

    _assert_refused(stage)


def test_the_raw_collection_must_answer_exactly_the_chunks_on_disk(stage) -> None:
    """An extra batch is an answer about material this run does not have."""

    def add_batch(payload: dict) -> None:
        extra = dict(payload["batches"][0])
        extra["chunk_id"] = "chunk_0001"
        extra["candidates"] = []
        payload["batches"].append(extra)

    directory = stage[0]
    _edit(directory.joinpath(RAW_CANDIDATES_FILENAME), add_batch)

    _assert_refused(stage)


def test_the_raw_collection_must_agree_with_the_stage_config_on_who_ran(stage) -> None:
    """Two artifacts naming two different executors describe two runs."""
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload.update({"model": "otro-modelo"}),
    )

    _assert_refused(stage)


def test_the_raw_proposal_count_must_match_the_candidate_funnel(stage) -> None:
    """counts.proposed is the funnel's own claim about how much it was given."""

    def drop_one(payload: dict) -> None:
        payload["batches"][0]["candidates"] = payload["batches"][0]["candidates"][:1]
        payload["proposed_count"] = 1

    directory = stage[0]
    _edit(directory.joinpath(RAW_CANDIDATES_FILENAME), drop_one)

    _assert_refused(stage)


def test_the_collection_limits_must_match_the_stage_configuration(stage) -> None:
    """A collection claiming a threshold the stage did not run under would make
    every rejection in it unexplainable."""
    directory = stage[0]
    _edit(
        directory.joinpath(CANDIDATES_FILENAME),
        lambda payload: payload.update({"min_score": 1.0}),
    )

    _assert_refused(stage)


def test_the_collection_duration_must_match_the_chunks(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(CANDIDATES_FILENAME),
        lambda payload: payload.update({"source_duration_seconds": 999.0}),
    )

    _assert_refused(stage)


# --- one model per collection ------------------------------------------------


def test_a_batch_cannot_name_a_model_the_collection_does_not(settings: Settings) -> None:
    """Constructed directly: the artifact must not be buildable in that shape."""
    batch = RawCandidateBatch(
        chunk_id="chunk_0000", candidates=[], raw_response="", model="otro-modelo"
    )

    with pytest.raises(ValueError, match="model"):
        RawCandidateCollection(
            rules_version=1,
            transcript_sha256="a" * 64,
            analyzer="fixture",
            analyzer_version="1",
            model="el-modelo",
            prompt_version="fake-fixture/v1",
            prompt_sha256="b" * 64,
            batches=[batch],
            proposed_count=0,
        )


def test_the_service_refuses_an_analyzer_that_answers_with_another_model(
    settings: Settings, tmp_path: Path
) -> None:
    """The identity recorded in every artifact comes from the adapter. An
    analyzer answering under a different name would file its output under an
    executor that did not produce it."""

    class Renamed:
        def find_candidates(self, chunk, context: AnalysisContext) -> CandidateBatch:
            return CandidateBatch(
                chunk_id=chunk.id, candidates=(), raw_response="", model="otro-modelo"
            )

    analyzer = FixtureAnalyzer(_fixture())
    plan = plan_analysis(speech_transcript(), settings, analyzer.identity)
    service = AnalysisService(Renamed(), analyzer.identity)
    directory = tmp_path.joinpath("analysis")

    with pytest.raises(AnalysisError, match="otro-modelo"):
        service.analyze(plan, directory, GENERATED_AT)


# --- the readers and the guard that should never fire ------------------------


def test_a_chunk_collection_from_another_schema_is_refused(stage) -> None:
    directory = stage[0]
    _edit(
        directory.joinpath(CHUNKS_FILENAME), lambda payload: payload.update({"schema_version": 99})
    )

    _assert_refused(stage, match="declares chunk schema")


def test_a_chunk_collection_with_impossible_content_is_refused(stage) -> None:
    """Right schema, wrong content: a chunk whose indices do not address the
    segments it holds."""
    directory = stage[0]
    _edit(
        directory.joinpath(CHUNKS_FILENAME),
        lambda payload: payload["chunks"][0].update({"segment_indices": [99]}),
    )

    _assert_refused(stage, match="not a valid chunk collection")


def test_the_service_refuses_to_write_artifacts_that_disagree(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four are built together, so this guard should never fire. It exists
    because the alternative to checking is writing an incoherent set that a
    later run reads back and believes, and a guard nobody exercises is a guard
    nobody knows works.
    """
    monkeypatch.setattr(
        "content_engine.services.analysis_service.coherence_problem",
        lambda *_: "the artifacts were built wrong",
    )
    analyzer = FixtureAnalyzer(_fixture())
    plan = plan_analysis(speech_transcript(), settings, analyzer.identity)
    service = AnalysisService(analyzer, analyzer.identity)
    directory = tmp_path.joinpath("analysis")

    with pytest.raises(AnalysisError, match="built wrong"):
        service.analyze(plan, directory, GENERATED_AT)

    assert not directory.exists() or list(directory.iterdir()) == []


# --- one chunking rules version across all four ------------------------------


def _coherence(outcome, **overrides):
    """Call the coherence check directly, with one artifact swapped.

    Some of these disagreements cannot be reached by editing a file: changing
    the stage configuration on disk breaks its digest first, so the refusal that
    fires is not the one under test. Testing the check itself is the honest
    level for those.
    """
    parts = {
        "transcript_sha256": outcome.chunks.transcript_sha256,
        "chunks": outcome.chunks,
        "raw": outcome.raw,
        "collection": outcome.collection,
        "config": outcome.stage_config,
    }
    parts.update(overrides)
    return coherence_problem(
        parts["transcript_sha256"],
        parts["chunks"],
        parts["raw"],
        parts["collection"],
        parts["config"],
    )


def test_a_coherent_set_of_artifacts_reports_no_problem(stage) -> None:
    assert _coherence(stage[1]) is None


def test_the_raw_collection_must_name_the_chunking_rules_the_chunks_used(stage) -> None:
    """All four artifacts declare it, so all four have to agree. Comparing only
    two of them left the other two free to claim the windows were cut by rules
    that produced something else."""
    outcome = stage[1]
    tampered = outcome.raw.model_copy(update={"rules_version": 99})

    problem = _coherence(outcome, raw=tampered)

    assert problem is not None
    assert "chunking rules" in problem


def test_the_stage_configuration_must_name_the_chunking_rules_the_chunks_used(
    stage,
) -> None:
    outcome = stage[1]
    tampered = outcome.stage_config.model_copy(update={"chunking_rules_version": 99})

    problem = _coherence(outcome, config=tampered)

    assert problem is not None
    assert "chunking rules" in problem


def test_the_candidates_must_name_the_chunking_rules_the_chunks_used(stage) -> None:
    outcome = stage[1]
    tampered = outcome.collection.model_copy(update={"rules_version": 99})

    problem = _coherence(outcome, collection=tampered)

    assert problem is not None
    assert "chunking rules" in problem


def test_the_chunks_must_name_the_rules_every_other_artifact_records(stage) -> None:
    outcome = stage[1]
    tampered = outcome.chunks.model_copy(update={"rules_version": 99})

    problem = _coherence(outcome, chunks=tampered)

    assert problem is not None
    assert "chunking rules" in problem


def test_an_edited_raw_rules_version_is_refused_on_disk(stage) -> None:
    """Reachable through a file: the coherence check runs before the fingerprint,
    so this is refused for saying the wrong thing rather than for having been
    touched at all."""
    directory = stage[0]
    _edit(
        directory.joinpath(RAW_CANDIDATES_FILENAME),
        lambda payload: payload.update({"rules_version": 99}),
    )

    _assert_refused(stage, match="chunking rules")
