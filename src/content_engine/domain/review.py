"""Domain models for human evaluation (CE-036 to CE-039).

Everything in the engine before this point is a machine's opinion. This is the
only place a person's judgement is recorded, which makes it the only artifact
that cannot be regenerated: a lost transcript costs GPU minutes, a lost set of
decisions costs somebody's afternoon and their attention.

That asymmetry is why the shapes here are strict to the point of being
inconvenient.

**Three decisions, three types.** The specification sketches one model with
``final_start``, ``final_end`` and ``reason`` all optional. Written that way, a
rejection can carry an approved interval, an approval can carry a rejection
reason, and an "edit" that moved nothing is indistinguishable from an approval.
Each of those is a record that lies about what a person did, and the point of
the artifact is to be evidence. So there are three models discriminated on
``decision``, and the fields that make no sense for a kind are absent from it
rather than null. ADR-029 records the deviation and why it is the smaller cost.

**A rejection has no final interval.** Not ``None``: absent. The requirement
lists final bounds among the fields every decision keeps, and for a rejection
there is nothing to keep -- no interval was approved. Storing the original
values there would produce a row that reads as an approved clip, which is
exactly the confusion CE-057's metrics are meant to measure.

**A human edit is not bound by the analyzer's duration policy.**
``min_duration_seconds`` and ``max_duration_seconds`` shape what the model may
propose (CE-030 enforces them on proposals). A person watching the preview is
the authority on where their clip ends, and widening their interval back to the
AI minimum would render something they did not choose. The only bounds an edit
must respect are the ones physics imposes: finite, ordered, and inside the
source.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from content_engine.domain.candidates import TIME_EPSILON, ValidatedCandidate, _Artifact
from content_engine.domain.enums import (
    REASON_REQUIRING_DETAIL,
    EditorialReason,
    ReviewDecisionType,
)
from content_engine.utils.canonical import canonical_sha256

#: Bumped whenever review/decisions.json changes incompatibly.
DECISIONS_SCHEMA_VERSION = 1
#: Bumped whenever review/config.effective.json changes incompatibly.
REVIEW_STAGE_CONFIG_SCHEMA_VERSION = 1
#: Bumped whenever the rules a decision must satisfy change, which invalidates
#: the comparison between two sessions' data rather than the data itself.
REVIEW_RULES_VERSION = 1
#: Bumped whenever the review fingerprint payload changes shape.
REVIEW_FINGERPRINT_VERSION = 1


def _require_utc(value: datetime, field: str) -> datetime:
    """Refuse a timestamp that is naive or in another zone.

    A review session is a sequence of moments that will be compared against
    each other and against stage timestamps from other machines. A naive
    timestamp is unorderable against an aware one, and a local one silently
    reorders the session when the file is read somewhere else.
    """
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(f"{field} has no timezone; timestamps are recorded in UTC")
    if offset.total_seconds() != 0:
        raise ValueError(f"{field} is offset from UTC by {offset}; timestamps are recorded in UTC")
    return value


class _Decision(_Artifact):
    """What every decision holds, whatever was decided.

    The original interval is kept on the decision itself rather than looked up
    later. The lookup would work only while the candidates file is still there
    and still the same one; the copy makes the record self-contained, and the
    collection checks the copy against the candidate so it cannot drift.
    """

    candidate_id: str = Field(min_length=1)
    original_start: float = Field(ge=0)
    original_end: float = Field(gt=0)
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "reviewed_at")

    @model_validator(mode="after")
    def validate_original(self) -> _Decision:
        if self.original_end <= self.original_start:
            raise ValueError(
                f"decision for {self.candidate_id} records an original interval ending at "
                f"{self.original_end}, at or before its start"
            )
        return self

    @property
    def final_interval(self) -> tuple[float, float] | None:
        """The interval a renderer should cut, or None when nothing was approved."""
        raise NotImplementedError  # pragma: no cover - every subclass answers


class ApprovedDecision(_Decision):
    """CE-036. The candidate is good as proposed.

    The final bounds are stored even though they are the original ones, because
    a renderer reads ``final_interval`` and should not have to know which kind
    of decision it is holding. They are checked for equality rather than
    derived, so a file claiming an approval with moved bounds is refused instead
    of being read as an edit.
    """

    decision: Literal[ReviewDecisionType.APPROVED] = ReviewDecisionType.APPROVED
    final_start: float = Field(ge=0)
    final_end: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> ApprovedDecision:
        moved = abs(self.final_start - self.original_start) > TIME_EPSILON or (
            abs(self.final_end - self.original_end) > TIME_EPSILON
        )
        if moved:
            raise ValueError(
                f"candidate {self.candidate_id} was approved with final bounds "
                f"[{self.final_start}, {self.final_end}] that differ from the proposed "
                f"[{self.original_start}, {self.original_end}]; a moved interval is an edit"
            )
        return self

    @property
    def final_interval(self) -> tuple[float, float]:
        return (self.final_start, self.final_end)


class RejectedDecision(_Decision):
    """CE-037. The candidate is not worth clipping.

    A reason is optional: making it mandatory would push a reviewer working
    through thirty candidates towards whichever label was least effort, and a
    guessed label is worse than a missing one for the metrics that will read
    this. ``other`` is the exception -- it names nothing, so the free text is
    the whole content and is required beside it.
    """

    decision: Literal[ReviewDecisionType.REJECTED] = ReviewDecisionType.REJECTED
    reason: EditorialReason | None = None
    detail: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self) -> RejectedDecision:
        if self.detail is not None and self.reason is None:
            raise ValueError(
                f"candidate {self.candidate_id} was rejected with a detail but no reason; "
                "the detail explains a reason rather than replacing it"
            )
        if self.reason is REASON_REQUIRING_DETAIL and not (self.detail or "").strip():
            raise ValueError(
                f"candidate {self.candidate_id} was rejected as "
                f"'{REASON_REQUIRING_DETAIL}', which carries no meaning without a detail"
            )
        return self

    @property
    def final_interval(self) -> None:
        """Nothing was approved, so there is no interval to render."""
        return None


class EditedDecision(_Decision):
    """CE-038. The moment is worth clipping, but not where the analyzer put it.

    Kept distinct from an approval on purpose. "Right idea, wrong boundary" and
    "right idea, right boundary" are different signals about the prompt, and
    ADR-010's snapping is judged by how often this decision has to be taken.
    """

    decision: Literal[ReviewDecisionType.EDITED] = ReviewDecisionType.EDITED
    final_start: float = Field(ge=0)
    final_end: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> EditedDecision:
        if self.final_end <= self.final_start:
            raise ValueError(
                f"candidate {self.candidate_id} was edited to end at {self.final_end}, at or "
                f"before its start ({self.final_start})"
            )
        moved = abs(self.final_start - self.original_start) > TIME_EPSILON or (
            abs(self.final_end - self.original_end) > TIME_EPSILON
        )
        if not moved:
            raise ValueError(
                f"candidate {self.candidate_id} is recorded as edited but its final bounds "
                f"[{self.final_start}, {self.final_end}] do not differ from the proposed "
                f"[{self.original_start}, {self.original_end}]; approve it instead"
            )
        return self

    @property
    def final_interval(self) -> tuple[float, float]:
        return (self.final_start, self.final_end)


#: The three kinds, discriminated by the value of ``decision``. Pydantic picks
#: the model from that field, so an unknown kind fails to parse rather than
#: falling back to whichever model happens to accept the payload.
ReviewDecision = Annotated[
    ApprovedDecision | RejectedDecision | EditedDecision,
    Field(discriminator="decision"),
]


class ReviewStageConfig(_Artifact):
    """``review/config.effective.json``: what the review stage ran against.

    The review stage has no encoder and no model, so its effective
    configuration is the identity of the material it was asked about and the
    rules its records had to satisfy. It exists for the same reason the other
    two stage configurations do: the manifest ties itself to a readable artifact
    rather than to an opaque digest alone.
    """

    schema_version: int = REVIEW_STAGE_CONFIG_SCHEMA_VERSION
    rules_version: int
    decisions_schema_version: int
    candidates_schema_version: int
    #: The analysis execution the decisions were taken over.
    analysis_fingerprint: str = Field(min_length=64, max_length=64)
    source_duration_seconds: float = Field(gt=0)
    selected_candidates: int = Field(ge=0)


class ReviewDecisionCollection(_Artifact):
    """``review/decisions.json``: every explicit decision, and nothing else.

    A skipped candidate has no entry. That is the whole difference between this
    file and a list of verdicts: absence means "not decided yet", so a session
    can be resumed, and no default is ever recorded on somebody's behalf.
    """

    schema_version: int = DECISIONS_SCHEMA_VERSION
    rules_version: int = REVIEW_RULES_VERSION
    #: Which analysis these decisions are about. A decision is meaningless
    #: against a different shortlist, so this is checked before anything is
    #: shown or resumed.
    analysis_fingerprint: str = Field(min_length=64, max_length=64)
    source_duration_seconds: float = Field(gt=0)
    created_at: datetime
    updated_at: datetime
    decisions: list[ReviewDecision] = Field(default_factory=list)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value, "timestamp")

    @model_validator(mode="after")
    def validate_collection(self) -> ReviewDecisionCollection:
        if self.updated_at < self.created_at:
            raise ValueError(
                f"updated_at ({self.updated_at}) precedes created_at ({self.created_at})"
            )
        identifiers = [decision.candidate_id for decision in self.decisions]
        repeated = sorted({name for name in identifiers if identifiers.count(name) > 1})
        if repeated:
            raise ValueError(
                f"candidate appears more than once: {', '.join(repeated)}. A candidate has "
                "one decision, and replacing it replaces the entry."
            )
        for decision in self.decisions:
            interval = decision.final_interval
            if interval is None:
                continue
            start, end = interval
            if end > self.source_duration_seconds + TIME_EPSILON:
                raise ValueError(
                    f"decision for {decision.candidate_id} ends at {end}, beyond the source "
                    f"duration ({self.source_duration_seconds})"
                )
            del start
        return self

    @property
    def by_candidate(self) -> dict[str, ApprovedDecision | RejectedDecision | EditedDecision]:
        return {decision.candidate_id: decision for decision in self.decisions}

    @property
    def counts(self) -> dict[str, int]:
        """One counter per kind, always all three, so a zero is visible."""
        tally = {kind.value: 0 for kind in ReviewDecisionType}
        for decision in self.decisions:
            tally[decision.decision.value] += 1
        return tally


def decisions_coherence_problem(
    collection: ReviewDecisionCollection,
    candidates: Sequence[ValidatedCandidate],
    analysis_fingerprint: str,
    source_duration_seconds: float,
) -> str | None:
    """The first way a decision file contradicts the shortlist it claims, or None.

    Four things can be wrong with a file that parses. It can belong to another
    analysis; it can describe a source of another length; it can hold a decision
    about a candidate that is not on the shortlist -- one that was rejected,
    deduplicated, cut by the cap, or never existed; and it can hold original
    bounds that are not the candidate's.

    The last one is the subtle one and the reason the originals are copied onto
    each decision at all. An edited original would make "the reviewer moved the
    boundary by four seconds" into a measurement of nothing, and CE-057 reads
    exactly that difference.
    """
    if collection.analysis_fingerprint != analysis_fingerprint:
        return (
            f"the decisions were taken over analysis {collection.analysis_fingerprint[:12]}, "
            f"but the run holds {analysis_fingerprint[:12]}"
        )
    if abs(collection.source_duration_seconds - source_duration_seconds) > TIME_EPSILON:
        return (
            f"the decisions were taken against a source duration of "
            f"{collection.source_duration_seconds} seconds and the run holds "
            f"{source_duration_seconds}"
        )

    selected = {candidate.id: candidate for candidate in candidates}
    for decision in collection.decisions:
        candidate = selected.get(decision.candidate_id)
        if candidate is None:
            return (
                f"there is a decision for {decision.candidate_id}, which is not one of the "
                f"{len(selected)} selected candidates"
            )
        if abs(decision.original_start - candidate.start) > TIME_EPSILON:
            return (
                f"the decision for {decision.candidate_id} records original_start "
                f"{decision.original_start} and the candidate starts at {candidate.start}"
            )
        if abs(decision.original_end - candidate.end) > TIME_EPSILON:
            return (
                f"the decision for {decision.candidate_id} records original_end "
                f"{decision.original_end} and the candidate ends at {candidate.end}"
            )
    return None


def pending_candidates(
    candidates: Sequence[ValidatedCandidate],
    collection: ReviewDecisionCollection,
) -> list[ValidatedCandidate]:
    """The selected candidates with no decision yet, in rank order."""
    decided = collection.by_candidate
    return [candidate for candidate in candidates if candidate.id not in decided]


def review_fingerprint(
    analysis_fingerprint: str,
    collection: ReviewDecisionCollection,
    config: ReviewStageConfig,
) -> str:
    """The integrity of one completed review.

    Recorded only when every selected candidate has a decision, so the digest
    identifies a finished editorial pass rather than a moment in the middle of
    one. Like the analysis fingerprint it covers the artifact, not only the
    inputs: the question it answers is whether the decisions on disk are the
    ones this run recorded.
    """
    return canonical_sha256(
        {
            "version": REVIEW_FINGERPRINT_VERSION,
            "analysis_fingerprint": analysis_fingerprint,
            "decisions": collection.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        }
    )


def review_stage_config_sha256(config: ReviewStageConfig) -> str:
    """Digest of the review stage configuration exactly as written to disk."""
    return canonical_sha256(config.model_dump(mode="json"))
