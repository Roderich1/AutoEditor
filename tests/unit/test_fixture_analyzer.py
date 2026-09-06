"""The fixture analyzer: what it replays, what it refuses, and what it never does.

The last of those matters most. This adapter stands where a provider will stand,
and the whole claim of this pull request is that nothing here calls one, reads a
credential or reaches the network.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from content_engine.adapters.analysis import fixture_analyzer as module
from content_engine.adapters.analysis.fixture_analyzer import (
    ANALYZER_NAME,
    FIXTURE_SCHEMA_VERSION,
    PROMPT_SHA256,
    PROMPT_VERSION,
    AnalysisFixture,
    FixtureAnalyzer,
    FixtureBatch,
    fixture_sha256,
    load_fixture,
    require_fixture_covers,
)
from content_engine.domain.exceptions import (
    AnalysisError,
    CorruptArtifactError,
    IncompatibleArtifactError,
    UnsupportedSchemaVersionError,
)
from content_engine.ports.analyzer import AnalysisContext, ContentAnalyzerPort
from tests.conftest import (
    analysis_fixture,
    chunk_of,
    raw_candidate,
    speech_transcript,
    write_fixture,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2].joinpath("src", "content_engine")
CHUNK = chunk_of(speech_transcript())


def _context(target: int = 10) -> AnalysisContext:
    return AnalysisContext(
        min_duration_seconds=20.0,
        max_duration_seconds=90.0,
        run_target_candidates=target,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256,
    )


def _fixture(**overrides: object) -> AnalysisFixture:
    batches = overrides.pop(
        "batches",
        [
            FixtureBatch(
                chunk_id="chunk_0000",
                raw_response='{"candidates": []}',
                candidates=[raw_candidate(10.0, 39.0)],
            )
        ],
    )
    return analysis_fixture(batches, **overrides)  # type: ignore[arg-type]


# --- replay ------------------------------------------------------------------


def test_the_fixture_analyzer_satisfies_the_port() -> None:
    analyzer: ContentAnalyzerPort = FixtureAnalyzer(_fixture())

    batch = analyzer.find_candidates(CHUNK, _context())

    assert batch.chunk_id == "chunk_0000"
    assert len(batch.candidates) == 1
    assert batch.candidates[0].start == 10.0


def test_the_raw_response_is_handed_on_exactly_as_recorded() -> None:
    """Down to the whitespace: it is the evidence of what a provider said."""
    odd = '  {"a": 1}\r\n\ttrailing  '
    analyzer = FixtureAnalyzer(
        _fixture(batches=[FixtureBatch(chunk_id="chunk_0000", raw_response=odd)])
    )

    batch = analyzer.find_candidates(CHUNK, _context())

    assert batch.raw_response == odd


def test_an_empty_answer_is_a_valid_answer() -> None:
    """A chunk with nothing worth clipping is a real outcome, not a failure."""
    analyzer = FixtureAnalyzer(
        _fixture(batches=[FixtureBatch(chunk_id="chunk_0000", raw_response="[]")])
    )

    batch = analyzer.find_candidates(CHUNK, _context())

    assert batch.candidates == ()


def test_the_batch_reports_the_model_that_answered() -> None:
    analyzer = FixtureAnalyzer(_fixture(model="fake-model-7"))

    assert analyzer.find_candidates(CHUNK, _context()).model == "fake-model-7"


def test_the_run_target_is_not_treated_as_a_quota_for_this_call() -> None:
    """ADR-021. A fake that sliced its answer by the run objective would bake
    the misreading into every fixture the later stages are verified against."""
    analyzer = FixtureAnalyzer(
        _fixture(
            batches=[
                FixtureBatch(
                    chunk_id="chunk_0000",
                    raw_response="[]",
                    candidates=[raw_candidate(10.0 + index, 39.0 + index) for index in range(4)],
                )
            ]
        )
    )

    for target in (1, 2, 10, 99):
        assert len(analyzer.find_candidates(CHUNK, _context(target)).candidates) == 4


def test_an_unanswered_chunk_is_refused_rather_than_answered_emptily() -> None:
    analyzer = FixtureAnalyzer(_fixture())
    other = chunk_of(speech_transcript(), chunk_id="chunk_0007")
    context = _context()

    with pytest.raises(IncompatibleArtifactError, match="no answer for chunk_0007"):
        analyzer.find_candidates(other, context)


def test_a_recorded_failure_is_raised_as_an_analysis_failure() -> None:
    """Exit code 5 and FAILED_ANALYSIS: the stage ran and the analyzer broke."""
    analyzer = FixtureAnalyzer(
        _fixture(batches=[FixtureBatch(chunk_id="chunk_0000", error="provider exploded")])
    )
    context = _context()

    with pytest.raises(AnalysisError, match="provider exploded"):
        analyzer.find_candidates(CHUNK, context)


# --- identity ----------------------------------------------------------------


def test_the_analyzer_names_itself_rather_than_the_configured_provider() -> None:
    identity = FixtureAnalyzer(_fixture(model="fake-model-7")).identity

    assert identity.analyzer == ANALYZER_NAME == "fixture"
    assert identity.model == "fake-model-7"
    assert "gemini" not in identity.analyzer.lower()


def test_the_prompt_identity_is_not_the_one_ce_026_will_own() -> None:
    """clip_candidates/v1 does not exist; claiming its hash would be a lie about
    a file nobody has written."""
    identity = FixtureAnalyzer(_fixture()).identity

    assert identity.prompt.version == "fake-fixture/v1"
    assert len(identity.prompt.sha256) == 64
    assert "clip_candidates" not in identity.prompt.version


def test_the_prompt_hash_is_derived_from_the_template_not_from_the_fixture() -> None:
    first = FixtureAnalyzer(_fixture(model="a-model")).identity
    second = FixtureAnalyzer(_fixture(model="another-model")).identity

    assert first.prompt.sha256 == second.prompt.sha256 == PROMPT_SHA256


def test_the_fixture_digest_changes_with_its_content() -> None:
    first = fixture_sha256(_fixture())
    second = fixture_sha256(
        _fixture(
            batches=[
                FixtureBatch(
                    chunk_id="chunk_0000",
                    raw_response='{"candidates": []}',
                    candidates=[raw_candidate(10.0, 40.0)],
                )
            ]
        )
    )

    assert first != second


def test_the_fixture_digest_ignores_how_the_file_is_formatted(tmp_path: Path) -> None:
    """Reindenting a fixture is not changing the experiment."""
    fixture = _fixture()
    compact = tmp_path.joinpath("compact.json")
    compact.write_text(
        json.dumps(fixture.model_dump(mode="json"), separators=(",", ":")), encoding="utf-8"
    )
    spaced = write_fixture(tmp_path.joinpath("spaced.json"), fixture)

    assert fixture_sha256(load_fixture(compact)) == fixture_sha256(load_fixture(spaced))


# --- loading -----------------------------------------------------------------


def test_a_fixture_round_trips_through_a_file(tmp_path: Path) -> None:
    path = write_fixture(tmp_path.joinpath("f.json"), _fixture())

    assert load_fixture(path) == _fixture()


def test_a_missing_fixture_is_an_expected_failure(tmp_path: Path) -> None:
    absent = tmp_path.joinpath("absent.json")

    with pytest.raises(CorruptArtifactError, match="does not exist"):
        load_fixture(absent)


def test_a_fixture_that_is_not_json_is_an_expected_failure(tmp_path: Path) -> None:
    path = tmp_path.joinpath("f.json")
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="cannot be read"):
        load_fixture(path)


def test_a_fixture_that_is_not_an_object_is_an_expected_failure(tmp_path: Path) -> None:
    path = tmp_path.joinpath("f.json")
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="does not contain"):
        load_fixture(path)


def test_a_fixture_from_another_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path.joinpath("f.json")
    payload = _fixture().model_dump(mode="json")
    payload["schema_version"] = FIXTURE_SCHEMA_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersionError, match="declares fixture schema"):
        load_fixture(path)


def test_a_fixture_with_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """Strict, so a typo in a key is a failure rather than a setting ignored."""
    path = tmp_path.joinpath("f.json")
    payload = _fixture().model_dump(mode="json")
    payload["temperature"] = 0.7
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="not a valid analysis fixture"):
        load_fixture(path)


def test_a_fixture_answering_one_chunk_twice_is_refused(tmp_path: Path) -> None:
    path = tmp_path.joinpath("f.json")
    payload = _fixture().model_dump(mode="json")
    payload["batches"] = [payload["batches"][0], payload["batches"][0]]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="same chunk more than once"):
        load_fixture(path)


def test_a_fixture_proposing_a_non_finite_timestamp_is_refused(tmp_path: Path) -> None:
    path = tmp_path.joinpath("f.json")
    path.write_text(
        '{"schema_version": 1, "model": "m", "batches": [{"chunk_id": "chunk_0000", '
        '"raw_response": "", "candidates": [{"start": NaN, "end": 39.0, '
        '"category": "explanation", "topic": "t", "hook": "h", "summary": "s", '
        '"reason": "r", "scores": {"hook": 1, "value": 1, "context_independence": 1, '
        '"clarity": 1, "engagement_potential": 1, "relevance": 1}}]}]}',
        encoding="utf-8",
    )

    with pytest.raises(CorruptArtifactError):
        load_fixture(path)


# --- coverage against the run ------------------------------------------------


def test_a_fixture_answering_every_chunk_is_accepted() -> None:
    require_fixture_covers(FixtureAnalyzer(_fixture()), ["chunk_0000"])


def test_a_fixture_answering_a_chunk_the_run_does_not_have_is_refused() -> None:
    """The dangerous one: ignoring it silently would look like it worked."""
    analyzer = FixtureAnalyzer(
        _fixture(
            batches=[
                FixtureBatch(chunk_id="chunk_0000", raw_response=""),
                FixtureBatch(chunk_id="chunk_0009", raw_response=""),
            ]
        )
    )

    with pytest.raises(IncompatibleArtifactError, match="does not have: chunk_0009"):
        require_fixture_covers(analyzer, ["chunk_0000"])


def test_a_fixture_missing_a_chunk_is_refused_before_the_first_call() -> None:
    analyzer = FixtureAnalyzer(_fixture())

    with pytest.raises(IncompatibleArtifactError, match="no answer for chunk_0001"):
        require_fixture_covers(analyzer, ["chunk_0000", "chunk_0001"])

    assert analyzer.calls == []


def test_a_run_with_no_chunks_needs_no_answers() -> None:
    """An empty transcript produces no chunks, so an empty fixture covers it."""
    require_fixture_covers(FixtureAnalyzer(analysis_fixture([])), [])


# --- what this adapter never does --------------------------------------------


def test_no_module_under_analysis_imports_a_provider_sdk() -> None:
    forbidden = {"openai", "google", "anthropic", "yt_dlp", "requests", "httpx", "socket"}
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}:{node.lineno} imports {name}"
                for name in names
                if name.split(".")[0] in forbidden
            )

    assert offenders == []


def test_the_adapter_reads_no_environment_variable() -> None:
    """Nothing here consults os.environ, so no credential can be read by accident."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert not attributes & {"environ", "getenv", "os"}


def test_replaying_a_fixture_leaves_the_credential_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set to a value this test would notice, then assert nothing consulted it."""
    seen: list[str] = []
    real_getenv = os.getenv
    monkeypatch.setattr(
        os, "getenv", lambda name, default=None: (seen.append(name), real_getenv(name, default))[1]
    )

    FixtureAnalyzer(_fixture()).find_candidates(CHUNK, _context())

    assert "GEMINI_API_KEY" not in seen


# --- a batch says one thing or the other, never both -------------------------


def test_a_batch_cannot_both_fail_and_return_candidates() -> None:
    """A recorded failure means the call did not produce candidates. A batch
    carrying both describes two outcomes, and whichever the replay honours, the
    other half is a silent instruction nobody reads.
    """
    candidates = [raw_candidate(10.0, 39.0)]

    with pytest.raises(ValidationError, match="failed"):
        FixtureBatch(
            chunk_id="chunk_0000",
            raw_response="",
            candidates=candidates,
            error="provider exploded",
        )


def test_a_failing_batch_may_still_keep_the_response_as_evidence() -> None:
    """The response is what the provider said before it was judged a failure,
    which is exactly the thing worth keeping about a failure."""
    batch = FixtureBatch(chunk_id="chunk_0000", raw_response="{ truncated", error="parse failed")

    assert batch.raw_response == "{ truncated"
    assert batch.candidates == []


def test_a_fixture_that_both_fails_and_returns_candidates_is_refused(tmp_path: Path) -> None:
    payload = _fixture().model_dump(mode="json")
    payload["batches"][0]["error"] = "provider exploded"
    path = tmp_path.joinpath("f.json")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="not a valid analysis fixture"):
        load_fixture(path)
