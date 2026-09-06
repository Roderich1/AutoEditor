"""Domain models for the Candidate Intelligence Engine (CE-023 to CE-033).

Kept apart from ``models.py``, which describes a run and its transcript. These
describe what an analyzer proposed and what the deterministic rules made of it,
and they are the types every rule in this subsystem consumes and returns.

The division that matters here is between what a model may assert and what only
code may decide. ``RawCandidate`` is untrusted output: the timestamps may be
invented and the duration may ignore the policy. ``ValidatedCandidate`` is what
survived the rules, carrying the reasons when it did not.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field, model_validator

from content_engine.domain.enums import (
    BoundaryAnchor,
    CandidateStatus,
    ClipCategory,
    RejectionReason,
)
from content_engine.domain.models import TranscriptSegment, _Model

#: Bumped whenever analysis/chunks.json changes incompatibly.
CHUNKS_SCHEMA_VERSION = 1
#: Bumped whenever analysis/candidates.raw.json changes incompatibly.
RAW_CANDIDATES_SCHEMA_VERSION = 1
#: Bumped whenever analysis/candidates.json changes incompatibly.
CANDIDATES_SCHEMA_VERSION = 1


class _Artifact(_Model):
    """Base for every candidate-engine model read back from disk."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TranscriptChunk(_Artifact):
    """A window of transcript handed to the analyzer, in absolute source time.

    Segments are never split. A segment straddling a window edge is included
    whole in every window it overlaps, so the segments inside the overlap appear
    in two consecutive chunks. That is what the overlap is for: an idea crossing
    a boundary must be visible complete to at least one call, or the analyzer can
    only ever see half of it.

    ``window_start``/``window_end`` are the nominal arithmetic window;
    ``start``/``end`` are the real extent of the segments that landed in it. They
    differ whenever a segment straddles an edge, and both are kept because the
    first explains how the chunk was cut and the second describes what the
    analyzer actually saw.
    """

    id: str = Field(min_length=1)
    index: int = Field(ge=0)
    window_start: float = Field(ge=0)
    window_end: float = Field(gt=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    #: Positions in ``Transcript.segments``, so a candidate traces back to source
    #: material without re-searching by timestamp.
    segment_indices: list[int]
    segments: list[TranscriptSegment]
    text: str

    @model_validator(mode="after")
    def validate_extent(self) -> TranscriptChunk:
        if self.window_end <= self.window_start:
            raise ValueError(
                f"chunk {self.index} window ends at {self.window_end}, at or before "
                f"its start ({self.window_start})"
            )
        if self.end <= self.start:
            raise ValueError(
                f"chunk {self.index} ends at {self.end}, at or before its start ({self.start})"
            )
        if not self.segments:
            raise ValueError(f"chunk {self.index} has no segments; empty chunks are not emitted")
        if len(self.segment_indices) != len(self.segments):
            raise ValueError(
                f"chunk {self.index} lists {len(self.segment_indices)} indices for "
                f"{len(self.segments)} segments"
            )
        if self.start != min(segment.start for segment in self.segments):
            raise ValueError(f"chunk {self.index} start is not the earliest segment start")
        if self.end != max(segment.end for segment in self.segments):
            raise ValueError(f"chunk {self.index} end is not the latest segment end")
        return self

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start


class ChunkCollection(_Artifact):
    """``analysis/chunks.json``."""

    schema_version: int = CHUNKS_SCHEMA_VERSION
    rules_version: int
    window_seconds: int = Field(gt=0)
    overlap_seconds: int = Field(ge=0)
    #: Ties the chunks to the exact transcript they were cut from.
    transcript_sha256: str = Field(min_length=64, max_length=64)
    source_duration_seconds: float = Field(gt=0)
    chunks: list[TranscriptChunk]


class CandidateScores(_Artifact):
    """The six dimensions the model rates, and nothing else.

    There is deliberately no total. ADR-008 puts the arithmetic in Python so the
    weights can be versioned and experimented with independently of the prompt,
    and so a model cannot inflate its own result. ``extra="forbid"`` makes a
    provider that returns one a loud validation failure rather than a value
    quietly ignored.
    """

    hook: int = Field(ge=0, le=100)
    value: int = Field(ge=0, le=100)
    context_independence: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)
    engagement_potential: int = Field(ge=0, le=100)
    relevance: int = Field(ge=0, le=100)


class RawCandidate(_Artifact):
    """One candidate exactly as proposed, before any rule has run.

    Untrusted by construction: the timestamps may be invented, the duration may
    ignore the policy, and the text is model output. Validating it is CE-030's
    job, not this model's.
    """

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    category: ClipCategory
    topic: str = Field(min_length=1, max_length=200)
    hook: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    scores: CandidateScores
    warnings: list[str] = Field(default_factory=list, max_length=20)


class BoundaryAdjustment(_Artifact):
    """What boundary snapping proposed, what it did, and whether it stuck.

    Keeping the proposed interval beside the adjusted one is what lets "correct
    idea, bad boundary" be measured separately from "wrong idea" once human
    review data exists.
    """

    proposed_start: float = Field(ge=0)
    proposed_end: float = Field(gt=0)
    adjusted_start: float = Field(ge=0)
    adjusted_end: float = Field(gt=0)
    start_delta: float
    end_delta: float
    start_anchor: BoundaryAnchor
    end_anchor: BoundaryAnchor
    window_seconds: float = Field(gt=0)
    #: True when snapping was undone because it pushed the candidate outside the
    #: duration policy. A good moment is not discarded over an editorial rule.
    reverted: bool = False


class ValidatedCandidate(_Artifact):
    """A candidate after the deterministic rules have run over it."""

    id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    #: Assigned by CE-033 to selected candidates only.
    rank: int | None = Field(default=None, ge=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    duration: float = Field(gt=0)
    category: ClipCategory
    topic: str
    hook: str
    summary: str
    reason: str
    scores: CandidateScores
    total_score: float = Field(ge=0, le=100)
    score_formula_version: int
    boundary: BoundaryAdjustment
    status: CandidateStatus
    rejection_reasons: list[RejectionReason] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_consistency(self) -> ValidatedCandidate:
        if self.end <= self.start:
            raise ValueError(f"candidate {self.id} ends at {self.end}, at or before its start")
        if abs(self.duration - (self.end - self.start)) > 1e-6:
            raise ValueError(
                f"candidate {self.id} declares duration {self.duration} for the interval "
                f"[{self.start}, {self.end}]"
            )
        if self.status is CandidateStatus.SUGGESTED and self.rejection_reasons:
            raise ValueError(f"candidate {self.id} is suggested but carries rejection reasons")
        if self.status is CandidateStatus.REJECTED and not self.rejection_reasons:
            raise ValueError(f"candidate {self.id} is rejected without a reason")
        if self.status is not CandidateStatus.SUGGESTED and self.rank is not None:
            raise ValueError(f"candidate {self.id} is ranked but was not selected")
        return self


class DeduplicationEvent(_Artifact):
    """One candidate dropped for covering the same moment as a better one."""

    kept_id: str = Field(min_length=1)
    dropped_id: str = Field(min_length=1)
    iou: float = Field(ge=0, le=1)
    kept_score: float = Field(ge=0, le=100)
    dropped_score: float = Field(ge=0, le=100)


class CandidateCounts(_Artifact):
    """Where the proposals went, so the funnel reads without being re-derived."""

    proposed: int = Field(ge=0)
    invalid: int = Field(ge=0)
    below_min_score: int = Field(ge=0)
    deduplicated: int = Field(ge=0)
    selected: int = Field(ge=0)


class CandidateCollection(_Artifact):
    """``analysis/candidates.json``.

    ``candidates`` holds only what survived, ranked. ``rejected`` holds
    everything else with its reason, because the ratio between them is the
    measurement that says whether the prompt is improving.
    """

    schema_version: int = CANDIDATES_SCHEMA_VERSION
    rules_version: int
    score_formula_version: int
    generated_at: datetime
    source_duration_seconds: float = Field(gt=0)
    #: The experimental objective. CE-033 caps output at ``max_candidates``; this
    #: is what the run aimed for, and what the prompt and the review UX use.
    target_candidates: int = Field(gt=0)
    #: The hard ceiling. Never exceeded, whatever the target says.
    max_candidates: int = Field(gt=0)
    min_score: float = Field(ge=0, le=100)
    dedupe_iou: float = Field(ge=0, le=1)
    boundary_snap_seconds: float = Field(gt=0)
    counts: CandidateCounts
    candidates: list[ValidatedCandidate]
    rejected: list[ValidatedCandidate]
    deduplication_events: list[DeduplicationEvent]

    @model_validator(mode="after")
    def validate_selection(self) -> CandidateCollection:
        if len(self.candidates) > self.max_candidates:
            raise ValueError(
                f"{len(self.candidates)} candidates exceed the hard cap of {self.max_candidates}"
            )
        # Every entry here carries a rank, and ValidatedCandidate already refuses
        # a rank on anything that was not selected, so "ranked implies suggested"
        # needs no second check: it would be unreachable.
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"ranks are not contiguous from 1: {ranks}")
        return self
