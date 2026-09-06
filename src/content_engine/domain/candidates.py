"""Domain models for the Candidate Intelligence Engine (CE-023 to CE-033).

Kept apart from ``models.py``, which describes a run and its transcript. These
describe what an analyzer proposed and what the deterministic rules made of it.

Two ideas shape everything here.

**A record must not be able to lie about what happened to it.** A proposal
refused before it was ever measured has no interval, no boundary and no total
score, because it never earned them. Rather than filling those fields with
invented values, such a proposal is a different type: ``InvalidCandidate``
preserves the parsed proposal, including the six ratings the provider supplied,
and says why it was refused. ``ValidatedCandidate`` is the record of something
that got far enough to be scored, and every one of its fields is therefore real.

Which reasons a record may cite follows from the same idea. A phase that never
ran cannot have decided anything, so ``InvalidCandidate`` is limited to the
reasons CE-030 can reach, and a scored record to the one terminal reason its
status allows. Without that rule a duplicate could be filed as a plain
rejection and counted under the wrong heading, with every total still adding
up.

**Untrusted output is preserved, not repaired.** ``RawCandidate`` accepts any
finite pair of timestamps, including negative, zero-length and inverted ones. A
model that invents impossible timestamps is exhibiting a measurable failure mode
of the prompt, and refusing the value at parse time would turn that measurement
into an exception with nothing recorded. What is refused is ``NaN`` and the
infinities, which are not timestamps at all.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field, model_validator

from content_engine.domain.enums import (
    BELOW_SCORE_REASON,
    PRE_SCORING_REASONS,
    TERMINAL_REASONS,
    TOP_N_REASON,
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

#: Timestamps are seconds held as binary floats, so equality between a value and
#: the arithmetic that produced it is only ever equality to within representation
#: error. A microsecond is far below anything that matters in a transcript and
#: far above the error these sums accumulate.
TIME_EPSILON = 1e-6

#: Totals are quantised to two decimals and an overlap is a ratio of two sums of
#: floats. Neither survives a JSON round trip as an exact bit pattern, so the
#: records are compared to the arithmetic that produced them, not equated to it.
SCORE_EPSILON = 1e-6
IOU_EPSILON = 1e-6


def _close(left: float, right: float, tolerance: float = TIME_EPSILON) -> bool:
    return abs(left - right) <= tolerance


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

        # The indices are the addresses of these exact segments in the transcript.
        # A mismatch would send anything tracing a candidate back to its source
        # to the wrong sentence, silently.
        for position, (index, segment) in enumerate(
            zip(self.segment_indices, self.segments, strict=True)
        ):
            if index != segment.index:
                raise ValueError(
                    f"chunk {self.index} position {position} lists index {index} but holds "
                    f"segment {segment.index}; the index does not match the segment"
                )
        for earlier, later in zip(self.segment_indices, self.segment_indices[1:], strict=False):
            if later <= earlier:
                raise ValueError(
                    f"chunk {self.index} indices are not strictly increasing: "
                    f"{earlier} is followed by {later}"
                )
        for first, second in zip(self.segments, self.segments[1:], strict=False):
            if second.start < first.start:
                raise ValueError(
                    f"chunk {self.index} segments are not in temporal order: "
                    f"{first.start} is followed by {second.start}"
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

    The timestamps carry no ordering or sign constraint on purpose. A negative
    start, a zero-length interval and an inverted one are all things a model
    really produces, and CE-030 exists to refuse them *with a recorded reason*.
    Refusing them here instead would replace a measurement with a parse error.

    ``NaN`` and the infinities stay refused through ``allow_inf_nan=False``: an
    impossible timestamp is data about the prompt, a non-number is not a
    timestamp at all.
    """

    start: float
    end: float
    category: ClipCategory
    topic: str = Field(min_length=1, max_length=200)
    hook: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    scores: CandidateScores
    warnings: list[str] = Field(default_factory=list, max_length=20)


class InvalidCandidate(_Artifact):
    """A proposal refused by CE-030, before it could be snapped or scored.

    It holds the parsed proposal and the reasons it was refused, and nothing
    else. There is no ``start``, no ``duration``, no ``boundary`` and no
    ``total_score``, because it never reached the phase that would have produced
    them — though ``proposed.scores`` still carries the six ratings the provider
    supplied, since those arrived with the proposal rather than being computed
    from it. Optional versions of the missing fields would let a caller ask a
    question that has no answer; filling them with plausible values would make
    the diagnosis record fiction.

    The truly verbatim response is ``CandidateBatch.raw_response``, which the
    port keeps exactly as the provider sent it. ``proposed`` is what survived
    parsing: unmodified, but structured.

    More than one reason is allowed, because a single CE-030 pass can find more
    than one defect in the same proposal. Every one of them must be a reason
    that pass could actually have reached.
    """

    id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    #: The proposal as parsed, with no field altered.
    proposed: RawCandidate
    rejection_reasons: list[RejectionReason] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_phase(self) -> InvalidCandidate:
        later = [reason for reason in self.rejection_reasons if reason not in PRE_SCORING_REASONS]
        if later:
            raise ValueError(
                f"candidate {self.id} was refused before scoring but cites "
                f"{', '.join(sorted(later))}, which can only be decided after a score exists"
            )
        return self


class BoundaryAdjustment(_Artifact):
    """What boundary snapping proposed, what it did, and whether it stuck.

    Only intervals that already passed CE-030 are snapped, so the proposed
    interval here is always well formed. Keeping it beside the adjusted one is
    what lets "correct idea, bad boundary" be measured separately from "wrong
    idea" once human review data exists, and the deltas are checked against the
    movement they claim to describe so the record cannot drift from the numbers.
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

    @model_validator(mode="after")
    def validate_movement(self) -> BoundaryAdjustment:
        if self.proposed_end <= self.proposed_start:
            raise ValueError(
                f"proposed interval [{self.proposed_start}, {self.proposed_end}] is not "
                "ordered; only a validated interval reaches snapping"
            )
        if self.adjusted_end <= self.adjusted_start:
            raise ValueError(
                f"adjusted interval [{self.adjusted_start}, {self.adjusted_end}] is not ordered"
            )

        if self.reverted:
            if not _close(self.adjusted_start, self.proposed_start) or not _close(
                self.adjusted_end, self.proposed_end
            ):
                raise ValueError(
                    "reverted adjustment must restore the proposed interval, but "
                    f"[{self.adjusted_start}, {self.adjusted_end}] is not "
                    f"[{self.proposed_start}, {self.proposed_end}]"
                )
            if not _close(self.start_delta, 0.0) or not _close(self.end_delta, 0.0):
                raise ValueError(
                    f"reverted adjustment must record zero deltas, got "
                    f"{self.start_delta} and {self.end_delta}"
                )
            if (
                self.start_anchor is not BoundaryAnchor.UNCHANGED
                or self.end_anchor is not BoundaryAnchor.UNCHANGED
            ):
                raise ValueError(
                    "reverted adjustment must report unchanged anchors, got "
                    f"{self.start_anchor} and {self.end_anchor}"
                )
            return self

        if not _close(self.start_delta, self.adjusted_start - self.proposed_start):
            raise ValueError(
                f"start_delta {self.start_delta} does not describe the move from "
                f"{self.proposed_start} to {self.adjusted_start}"
            )
        if not _close(self.end_delta, self.adjusted_end - self.proposed_end):
            raise ValueError(
                f"end_delta {self.end_delta} does not describe the move from "
                f"{self.proposed_end} to {self.adjusted_end}"
            )
        return self


class ValidatedCandidate(_Artifact):
    """A candidate that reached scoring, whatever became of it afterwards.

    Every field is real by the time this exists: the interval passed CE-030, the
    boundary was computed by CE-031 and the total by CE-025. A candidate that
    died before any of that is an ``InvalidCandidate`` instead, which is why
    nothing here is optional except the rank.
    """

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
        if not _close(self.duration, self.end - self.start):
            raise ValueError(
                f"candidate {self.id} declares duration {self.duration} for the interval "
                f"[{self.start}, {self.end}]"
            )

        # The interval is the boundary's output. Letting them disagree would mean
        # the clip that gets rendered is not the one the adjustment describes.
        if not _close(self.start, self.boundary.adjusted_start):
            raise ValueError(
                f"candidate {self.id} starts at {self.start} but its boundary "
                f"adjusted_start is {self.boundary.adjusted_start}"
            )
        if not _close(self.end, self.boundary.adjusted_end):
            raise ValueError(
                f"candidate {self.id} ends at {self.end} but its boundary "
                f"adjusted_end is {self.boundary.adjusted_end}"
            )

        if self.status is CandidateStatus.SUGGESTED:
            if self.rejection_reasons:
                raise ValueError(f"candidate {self.id} is suggested but carries rejection reasons")
            return self

        if not self.rejection_reasons:
            raise ValueError(f"candidate {self.id} is {self.status} without a reason")
        if self.rank is not None:
            raise ValueError(f"candidate {self.id} is ranked but was not selected")

        # A terminal outcome happened once, so it has one cause, so it belongs
        # to exactly one counter. A list of two would leave the funnel free to
        # file the same candidate under whichever heading suited it.
        if len(self.rejection_reasons) != 1:
            raise ValueError(
                f"candidate {self.id} is {self.status} with {len(self.rejection_reasons)} "
                "reasons; a terminal outcome has exactly one"
            )
        allowed = TERMINAL_REASONS[self.status]
        reason = self.rejection_reasons[0]
        if reason not in allowed:
            raise ValueError(
                f"candidate {self.id} is {self.status} citing {reason}, which is not a "
                f"terminal reason for that status; expected one of {', '.join(sorted(allowed))}"
            )
        return self


class DeduplicationEvent(_Artifact):
    """One candidate dropped for covering the same moment as a better one.

    The event restates facts that are already on both candidates: their totals,
    and the overlap between their intervals. That is deliberate — it is the
    audit record of a decision — but it means the event can disagree with them,
    so ``CandidateCollection`` checks every field against the two records it
    names. What cannot be checked there is checked here: an event naming the
    same candidate on both sides describes no decision at all.
    """

    kept_id: str = Field(min_length=1)
    dropped_id: str = Field(min_length=1)
    iou: float = Field(ge=0, le=1)
    kept_score: float = Field(ge=0, le=100)
    dropped_score: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_sides(self) -> DeduplicationEvent:
        if self.kept_id == self.dropped_id:
            raise ValueError(
                f"deduplication event names {self.kept_id} as both kept and dropped; "
                "a candidate cannot deduplicate itself"
            )
        return self


def _interval_iou(first: ValidatedCandidate, second: ValidatedCandidate) -> float:
    """Intersection over union of two candidate intervals.

    Written here rather than imported from CE-032 on purpose: this is the
    definition the record is held to, and sharing a helper with the algorithm
    would let a change in how deduplication decides silently redefine what its
    own audit trail claims.
    """
    overlap = min(first.end, second.end) - max(first.start, second.start)
    if overlap <= 0:
        return 0.0
    union = (first.end - first.start) + (second.end - second.start) - overlap
    return overlap / union


class CandidateCounts(_Artifact):
    """Where every proposal ended up.

    These are **terminal outcomes, mutually exclusive by construction**. Every
    proposal the analyzer returned reaches exactly one of them, so the five
    outcome counters sum to ``proposed`` and that identity is enforced. They are
    not overlapping tallies of things that happened along the way.

    ``proposed``         every RawCandidate returned, across all chunks.
    ``invalid``          refused by CE-030 before scoring. One per InvalidCandidate.
    ``below_min_score``  scored, then dropped for falling under ``min_score``.
    ``deduplicated``     scored, above the threshold, dropped as a duplicate.
    ``not_in_top_n``     survived everything and was still cut by ``max_candidates``.
    ``selected``         ranked and emitted. One per entry in ``candidates``.

    The pipeline order is what makes them exclusive: the minimum-score filter
    runs before deduplication, so a candidate below the threshold is never also
    counted as a duplicate, and the top-N cut runs last over what is left.

    Every one of these is read back off the records by ``CandidateCollection``,
    not merely required to add up. A sum that balances can still have two
    categories swapped, and the difference between "the prompt scores badly" and
    "the cap is too tight" is exactly the kind of thing this file exists to
    keep honest.
    """

    proposed: int = Field(ge=0)
    invalid: int = Field(ge=0)
    below_min_score: int = Field(ge=0)
    deduplicated: int = Field(ge=0)
    not_in_top_n: int = Field(ge=0)
    selected: int = Field(ge=0)


class CandidateCollection(_Artifact):
    """``analysis/candidates.json``.

    Three lists, split by how far a proposal got rather than by how good it was.
    ``candidates`` holds what was selected, ranked. ``rejected`` holds what was
    scored and then dropped. ``invalid`` holds what never reached scoring at all.
    Nothing is discarded, because the ratio between them is the measurement that
    says whether a prompt is improving.
    """

    schema_version: int = CANDIDATES_SCHEMA_VERSION
    rules_version: int
    score_formula_version: int
    generated_at: datetime
    source_duration_seconds: float = Field(gt=0)
    #: The objective for the **whole run**, never a quota per chunk. CE-033 caps
    #: output at ``max_candidates``; this is what the run was aiming for, and
    #: what the review UX and the experiment record use.
    target_candidates: int = Field(gt=0)
    #: The hard ceiling for the whole run. Never exceeded, whatever the target says.
    max_candidates: int = Field(gt=0)
    min_score: float = Field(ge=0, le=100)
    dedupe_iou: float = Field(ge=0, le=1)
    boundary_snap_seconds: float = Field(gt=0)
    counts: CandidateCounts
    candidates: list[ValidatedCandidate]
    rejected: list[ValidatedCandidate]
    invalid: list[InvalidCandidate]
    deduplication_events: list[DeduplicationEvent]

    @model_validator(mode="after")
    def validate_funnel(self) -> CandidateCollection:
        # Every entry in `candidates` carries a rank, and ValidatedCandidate
        # already refuses a rank on anything not selected, so "ranked implies
        # suggested" needs no check of its own here: it would be unreachable.
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"ranks are not contiguous from 1: {ranks}")
        if len(self.candidates) > self.max_candidates:
            raise ValueError(
                f"{len(self.candidates)} candidates exceed the hard cap of {self.max_candidates}"
            )

        for candidate in self.rejected:
            if candidate.status is CandidateStatus.SUGGESTED:
                raise ValueError(f"candidate {candidate.id} is suggested but appears in rejected")

        scored_ids = [candidate.id for candidate in (*self.candidates, *self.rejected)]
        all_ids = [*scored_ids, *(candidate.id for candidate in self.invalid)]
        duplicates = sorted({name for name in all_ids if all_ids.count(name) > 1})
        if duplicates:
            raise ValueError(f"identifier appears more than once: {', '.join(duplicates)}")

        scored = {candidate.id: candidate for candidate in (*self.candidates, *self.rejected)}
        self._validate_deduplication(scored)

        below_min_score = [
            candidate
            for candidate in self.rejected
            if candidate.rejection_reasons == [BELOW_SCORE_REASON]
        ]
        not_in_top_n = [
            candidate
            for candidate in self.rejected
            if candidate.rejection_reasons == [TOP_N_REASON]
        ]
        deduplicated = [
            candidate
            for candidate in self.rejected
            if candidate.status is CandidateStatus.DEDUPLICATED
        ]

        # Each counter is read off the records rather than balanced against the
        # others. Checking only that the three sum to len(rejected) would accept
        # any permutation of them, which is the failure this file is here to
        # prevent: a duplicate filed as a score failure changes what the funnel
        # says about the prompt while every total still adds up.
        for name, counted, records, described in (
            ("selected", self.counts.selected, self.candidates, "selected candidates"),
            ("invalid", self.counts.invalid, self.invalid, "invalid candidates"),
            (
                "below_min_score",
                self.counts.below_min_score,
                below_min_score,
                "candidates rejected below the minimum score",
            ),
            (
                "deduplicated",
                self.counts.deduplicated,
                deduplicated,
                "deduplicated candidates",
            ),
            (
                "not_in_top_n",
                self.counts.not_in_top_n,
                not_in_top_n,
                "candidates cut by the cap",
            ),
        ):
            if counted != len(records):
                raise ValueError(f"counts.{name} is {counted} for {len(records)} {described}")

        # counts.deduplicated == len(deduplication_events) needs no check of its
        # own: _validate_deduplication pairs every deduplicated record with
        # exactly one event and refuses an event that drops anything else, so
        # the two are the same number by then.
        total = (
            self.counts.invalid
            + self.counts.below_min_score
            + self.counts.deduplicated
            + self.counts.not_in_top_n
            + self.counts.selected
        )
        if self.counts.proposed != total:
            raise ValueError(
                f"counts.proposed is {self.counts.proposed} but the terminal outcomes "
                f"sum to {total}"
            )
        return self

    def _validate_deduplication(self, scored: dict[str, ValidatedCandidate]) -> None:
        """Hold every event to the two records it names.

        The event exists to make a removal auditable, so an event nobody can
        check is worse than none: it reads as evidence. Each field is therefore
        compared against the candidates themselves, and the pipeline order —
        minimum score, then deduplication, then the top-N cut — says which
        candidates could have been on either side. A keeper may end up suggested
        or cut by the cap afterwards, but it can never be one that the score
        filter had already removed, nor one that deduplication itself dropped.
        """
        dropped_by_event: set[str] = set()
        for event in self.deduplication_events:
            unknown = {event.kept_id, event.dropped_id} - set(scored)
            if unknown:
                raise ValueError(
                    f"deduplication event references unknown candidates: "
                    f"{', '.join(sorted(unknown))}"
                )
            kept = scored[event.kept_id]
            dropped = scored[event.dropped_id]

            if dropped.status is not CandidateStatus.DEDUPLICATED:
                raise ValueError(
                    f"deduplication event drops {event.dropped_id}, which is not recorded "
                    f"as deduplicated but as {dropped.status}"
                )
            if event.dropped_id in dropped_by_event:
                raise ValueError(
                    f"candidate {event.dropped_id} is removed by more than one deduplication event"
                )
            dropped_by_event.add(event.dropped_id)

            if kept.status is CandidateStatus.DEDUPLICATED or kept.rejection_reasons == [
                BELOW_SCORE_REASON
            ]:
                raise ValueError(
                    f"deduplication event keeps {event.kept_id}, which did not survive "
                    f"deduplication itself: it is recorded as {kept.status} for "
                    f"{', '.join(sorted(kept.rejection_reasons))}"
                )

            if not _close(event.kept_score, kept.total_score, SCORE_EPSILON):
                raise ValueError(
                    f"deduplication event records kept_score {event.kept_score} for "
                    f"{event.kept_id}, which scored {kept.total_score}"
                )
            if not _close(event.dropped_score, dropped.total_score, SCORE_EPSILON):
                raise ValueError(
                    f"deduplication event records dropped_score {event.dropped_score} for "
                    f"{event.dropped_id}, which scored {dropped.total_score}"
                )
            if event.kept_score < event.dropped_score - SCORE_EPSILON:
                raise ValueError(
                    f"deduplication event keeps {event.kept_id} at {event.kept_score}, "
                    f"scored below the {event.dropped_score} of {event.dropped_id}, "
                    "which it dropped"
                )
            if _close(event.kept_score, event.dropped_score, SCORE_EPSILON) and (
                kept.start,
                kept.id,
            ) > (dropped.start, dropped.id):
                raise ValueError(
                    f"deduplication event keeps {event.kept_id} over {event.dropped_id} at "
                    "the same score; a tie is broken by the earlier start and then by the "
                    f"identifier, which keeps {event.dropped_id}"
                )

            overlap = _interval_iou(kept, dropped)
            if not _close(event.iou, overlap, IOU_EPSILON):
                raise ValueError(
                    f"deduplication event records iou {event.iou} for {event.kept_id} and "
                    f"{event.dropped_id}, whose intervals overlap at {overlap}"
                )
            if event.iou < self.dedupe_iou:
                raise ValueError(
                    f"deduplication event records iou {event.iou} for {event.dropped_id}, "
                    f"below the deduplication threshold of {self.dedupe_iou}"
                )

        unevidenced = sorted(
            candidate.id
            for candidate in self.rejected
            if candidate.status is CandidateStatus.DEDUPLICATED
            and candidate.id not in dropped_by_event
        )
        if unevidenced:
            raise ValueError(
                f"recorded as deduplicated but no deduplication event removes them: "
                f"{', '.join(unevidenced)}"
            )
