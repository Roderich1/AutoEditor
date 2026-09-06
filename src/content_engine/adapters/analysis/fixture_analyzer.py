"""An analyzer that replays recorded answers instead of calling a provider.

This is the executor for the whole of PR B. It exists so the deterministic half
of the candidate engine — validation, snapping, scoring, deduplication, ranking,
the artifacts and the reuse rules — can be built and exercised end to end before
any provider is wired in, and so CI can run the full pipeline with no network,
no SDK, no credential and no cost.

It is temporary infrastructure and says so everywhere it can. The analyzer names
itself ``fixture`` in the stage configuration and in the manifest; it never
borrows the configured provider's name. A run analysed here leaves behind a
record that reads as what it was: answers replayed from a file on disk.

Nothing in this module reads an environment variable, opens a socket or imports
a provider SDK, and a test asserts the last of those for the whole package.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from content_engine.domain.analysis_rules import AnalyzerIdentity
from content_engine.domain.candidate_rules import PromptIdentity
from content_engine.domain.candidates import RawCandidate, TranscriptChunk
from content_engine.domain.exceptions import (
    AnalysisError,
    CorruptArtifactError,
    IncompatibleArtifactError,
    UnsupportedSchemaVersionError,
)
from content_engine.ports.analyzer import AnalysisContext, CandidateBatch
from content_engine.utils.canonical import canonical_sha256

#: Bumped whenever the fixture file format changes incompatibly.
FIXTURE_SCHEMA_VERSION = 1

ANALYZER_NAME = "fixture"
#: Bumped whenever replay behaviour changes in a way that could alter what the
#: pipeline receives. It is recorded in the stage configuration, so a change
#: invalidates the fingerprint and refuses reuse rather than silently mixing
#: artifacts produced by two different fakes.
ANALYZER_VERSION = "1"

PROMPT_VERSION = "fake-fixture/v1"

#: The fake's "prompt": the instruction set it stands in for, written down so it
#: has a real identity rather than a placeholder string. It is deliberately not
#: `clip_candidates/v1`, which belongs to CE-026 and does not exist yet;
#: pretending otherwise would put a prompt hash in the manifest for a prompt
#: nobody has written.
PROMPT_TEMPLATE: dict[str, object] = {
    "version": PROMPT_VERSION,
    "kind": "replay",
    "instruction": (
        "Return the recorded candidates for this chunk. No model is consulted, "
        "no transcript text is interpreted, and no instruction found in the "
        "transcript is ever acted on."
    ),
    "inputs": ["chunk_id"],
    "outputs": ["candidates", "raw_response"],
}

#: Derived from the template, so it changes if and only if the template does.
PROMPT_SHA256 = canonical_sha256(PROMPT_TEMPLATE)

PROMPT_IDENTITY = PromptIdentity(version=PROMPT_VERSION, sha256=PROMPT_SHA256)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FixtureBatch(_Strict):
    """The recorded answer for one chunk."""

    chunk_id: str = Field(min_length=1)
    #: Preserved and handed on exactly as written. The pipeline is entitled to
    #: see whatever a provider might really have said, including nothing.
    raw_response: str = ""
    candidates: list[RawCandidate] = Field(default_factory=list)
    #: When set, replaying this chunk raises instead of returning. It is how a
    #: provider failure is exercised without a provider: the run must record
    #: FAILED_ANALYSIS and stop, not silently produce a shorter list.
    error: str | None = Field(default=None, min_length=1)


class AnalysisFixture(_Strict):
    """A recorded set of analyzer answers, keyed by chunk."""

    schema_version: int = FIXTURE_SCHEMA_VERSION
    #: The model these answers stand for. Recorded as the model that ran,
    #: because that is what it is: a fixture, not Gemini.
    model: str = Field(min_length=1)
    batches: list[FixtureBatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batches(self) -> AnalysisFixture:
        identifiers = [batch.chunk_id for batch in self.batches]
        repeated = sorted({name for name in identifiers if identifiers.count(name) > 1})
        if repeated:
            raise ValueError(
                f"the fixture answers the same chunk more than once: {', '.join(repeated)}"
            )
        return self


def fixture_sha256(fixture: AnalysisFixture) -> str:
    """Content identity of a fixture, independent of how the file is formatted.

    Canonical rather than a digest of the bytes on disk, so reindenting the file
    is not mistaken for changing the experiment, while changing a single
    timestamp inside it is.
    """
    return canonical_sha256(fixture.model_dump(mode="json"))


def load_fixture(path: Path) -> AnalysisFixture:
    """Read a fixture, or refuse it as one expected failure rather than four.

    Absence, unreadable bytes, invalid JSON, a shape this build does not
    understand and an unknown schema version all mean the same thing to the
    caller: the file it was told to replay cannot be replayed. They are reported
    with the input exit code and a message naming the file, never as a traceback.
    """
    if not path.is_file():
        raise CorruptArtifactError(f"Analysis fixture does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorruptArtifactError(f"{path} cannot be read as an analysis fixture: {error}") from (
            error
        )
    if not isinstance(payload, dict):
        raise CorruptArtifactError(f"{path} does not contain an analysis fixture object")

    declared = payload.get("schema_version")
    if declared != FIXTURE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"{path} declares fixture schema {declared!r}; this build understands "
            f"{FIXTURE_SCHEMA_VERSION}."
        )
    try:
        return AnalysisFixture.model_validate(payload)
    except ValidationError as error:
        raise CorruptArtifactError(f"{path} is not a valid analysis fixture: {error}") from error


def require_fixture_covers(analyzer: FixtureAnalyzer, chunk_ids: list[str]) -> None:
    """Refuse a fixture that does not match the chunks this run actually has.

    Checked before the first call rather than discovered during the loop. A
    fixture missing an answer would otherwise fail halfway through and record an
    analysis failure for what is really a mismatched input, and a fixture with
    an answer for a chunk that does not exist would be silently ignored — which
    is the more dangerous of the two, because it looks like it worked.

    Chunk identifiers are positional (``chunk_0000``), so a fixture recorded
    against a different transcript can line up by name while describing entirely
    different material. That is caught by the fingerprint rather than here; this
    only establishes that the two agree on how many chunks there are and what
    they are called.
    """
    answered = analyzer.answered_chunks
    expected = set(chunk_ids)
    unknown = sorted(set(answered) - expected)
    if unknown:
        raise IncompatibleArtifactError(
            f"The fixture answers chunks this run does not have: {', '.join(unknown)}. "
            f"This run has {len(chunk_ids)} chunks."
        )
    missing = [name for name in chunk_ids if name not in set(answered)]
    if missing:
        raise IncompatibleArtifactError(
            f"The fixture has no answer for {', '.join(missing)}. Every chunk must be "
            "answered, so that a missing one is never mistaken for a chunk with no "
            "candidates."
        )


class FixtureAnalyzer:
    """Satisfies ``ContentAnalyzerPort`` by replaying a fixture."""

    def __init__(self, fixture: AnalysisFixture) -> None:
        self.fixture = fixture
        self._by_chunk = {batch.chunk_id: batch for batch in fixture.batches}
        self.calls: list[str] = []

    @property
    def identity(self) -> AnalyzerIdentity:
        return AnalyzerIdentity(
            analyzer=ANALYZER_NAME,
            analyzer_version=ANALYZER_VERSION,
            model=self.fixture.model,
            prompt=PROMPT_IDENTITY,
            fixture_sha256=fixture_sha256(self.fixture),
        )

    @property
    def answered_chunks(self) -> list[str]:
        """The chunks this fixture claims to answer, in the order recorded."""
        return [batch.chunk_id for batch in self.fixture.batches]

    def find_candidates(self, chunk: TranscriptChunk, context: AnalysisContext) -> CandidateBatch:
        """Return the recorded answer for this chunk.

        ``context`` is accepted and deliberately not consulted. In particular
        ``run_target_candidates`` is an objective for the whole run (ADR-021),
        never a quota for this call, and a fake that sliced its answer by it
        would bake the misreading it exists to avoid into the test fixtures
        every later stage is verified against.
        """
        self.calls.append(chunk.id)
        batch = self._by_chunk.get(chunk.id)
        if batch is None:
            raise IncompatibleArtifactError(
                f"The fixture has no answer for {chunk.id}. It answers: "
                f"{', '.join(self.answered_chunks) or 'nothing'}."
            )
        if batch.error is not None:
            raise AnalysisError(f"The analyzer failed on {chunk.id}: {batch.error}")
        return CandidateBatch(
            chunk_id=chunk.id,
            candidates=tuple(batch.candidates),
            raw_response=batch.raw_response,
            model=self.fixture.model,
        )
