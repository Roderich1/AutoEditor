"""The content analysis boundary (CE-027).

A Protocol over domain types and nothing else. No provider SDK is imported here,
no provider is named, and no implementation lives in this module: the Gemini
adapter of ADR-019 arrives behind this interface, and an OpenAI one could arrive
beside it without a line of the domain changing.

That neutrality is the point rather than a nicety. Everything downstream of this
call — timestamp validation, boundary snapping, scoring, deduplication, ranking —
is deterministic code that consumes intervals and six integers. It cannot tell
which model produced them, and it must never be able to. Swapping the provider
is then an experiment, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from content_engine.domain.candidates import RawCandidate, TranscriptChunk


@dataclass(frozen=True)
class AnalysisContext:
    """Everything an analyzer is told that does not come from the transcript.

    Separated from the chunk deliberately. The chunk is untrusted data spoken by
    a person; this is instruction from the operator. Keeping them apart in the
    type system is the first half of the rule that spoken content can never
    become an instruction, and an adapter is expected to keep them apart in the
    request it builds too.
    """

    #: Duration policy the analyzer is asked to respect. Advisory: the model
    #: cannot be trusted to obey it, so CE-030 enforces it regardless.
    min_duration_seconds: float
    max_duration_seconds: float
    #: How many candidates the run is aiming for per chunk. An objective, not a
    #: cap; the hard ceiling is applied by CE-033 over the whole run.
    target_candidates: int
    #: Recorded on every run so a change in either is visible in the manifest.
    prompt_version: str
    prompt_sha256: str


@dataclass(frozen=True)
class CandidateBatch:
    """What one analyzer call produced for one chunk.

    ``raw_response`` is kept verbatim so a parse failure still leaves evidence on
    disk, and so a disagreement between what the provider said and what the
    domain made of it can be investigated after the fact.
    """

    chunk_id: str
    candidates: tuple[RawCandidate, ...]
    raw_response: str
    model: str


class ContentAnalyzerPort(Protocol):
    """Proposes candidate segments for one transcript chunk.

    An implementation may not compute a total score, may not act on anything the
    transcript says, and may not expose provider types to its caller. It returns
    proposals; whether any of them survives is decided elsewhere.
    """

    def find_candidates(
        self,
        chunk: TranscriptChunk,
        context: AnalysisContext,
    ) -> CandidateBatch: ...
