"""The deterministic half of the Candidate Intelligence Engine (CE-030 to CE-033).

Everything here is a pure function over domain types. No I/O, no clock, no
provider, no randomness, no iteration over a set or a dict whose order is not
already fixed by the caller. Given the same chunks and the same proposals, this
module returns the same collection, in the same order, on any machine.

That is the point rather than a nicety. ADR-003 puts semantic judgement in the
model and execution in code; if this half were not reproducible, an experiment
could not attribute a change in the shortlist to the prompt, and the prompt is
the thing the project is trying to measure.

The pipeline order is binding and is the reason the outcome categories can be
mutually exclusive at all:

    proposals from every chunk
      -> CE-030 validation            invalid, before anything is computed
      -> CE-031 boundary snapping     only for intervals that survived
      -> CE-025 deterministic score
      -> minimum-score filter         below_min_score
      -> CE-032 global deduplication  deduplicated
      -> CE-033 global ranking
      -> max_candidates ceiling       not_in_top_n
      -> selected

Nothing is discarded silently at any step. Every proposal leaves this module in
exactly one of those five states, with the record that explains it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    TIME_EPSILON,
    BoundaryAdjustment,
    CandidateCollection,
    CandidateCounts,
    DeduplicationEvent,
    InvalidCandidate,
    RawCandidate,
    TranscriptChunk,
    ValidatedCandidate,
)
from content_engine.domain.enums import BoundaryAnchor, CandidateStatus, RejectionReason
from content_engine.domain.scoring import SCORE_FORMULA_VERSION, calculate_total
from content_engine.utils.canonical import canonical_sha256

#: Bumped whenever CE-030 changes which proposals it refuses or why.
VALIDATION_RULES_VERSION = 1
#: Bumped whenever CE-031 changes where a boundary is moved to.
BOUNDARY_RULES_VERSION = 1
#: Bumped whenever CE-032 changes what counts as a duplicate or which one is kept.
DEDUPE_RULES_VERSION = 1
#: Bumped whenever CE-033 changes the order or the ceiling.
RANKING_RULES_VERSION = 1
#: Bumped whenever the identifier payload changes. Every existing identifier
#: changes with it, which is why it is versioned separately from the rules.
CANDIDATE_ID_VERSION = 1

#: Totals are quantised to two decimals; a threshold comparison at exactly the
#: boundary must not turn on the last bit of a binary float.
SCORE_EPSILON = 1e-6
#: An overlap ratio is a quotient of sums of floats, so the duplicate threshold
#: is met "at or above, within representation error".
IOU_EPSILON = 1e-9

#: The order CE-030 reports its findings in. Fixed so the same defective
#: proposal produces the same record every time, and so a diff between two runs
#: is a difference in the data rather than in iteration order.
REASON_ORDER: tuple[RejectionReason, ...] = (
    RejectionReason.INVALID_INTERVAL,
    RejectionReason.OUTSIDE_CHUNK,
    RejectionReason.END_BEYOND_SOURCE,
    RejectionReason.TOO_SHORT,
    RejectionReason.TOO_LONG,
    RejectionReason.UNGROUNDED,
)


@dataclass(frozen=True)
class CandidatePolicy:
    """The settings the deterministic rules are asked to enforce.

    A snapshot rather than the live ``CandidateSettings``: these rules are pure,
    and reading configuration in the middle of them would make the result depend
    on something the caller cannot see in the arguments.
    """

    min_duration_seconds: float
    max_duration_seconds: float
    min_score: float
    target_candidates: int
    max_candidates: int
    dedupe_iou: float
    boundary_snap_seconds: float


@dataclass(frozen=True)
class PromptIdentity:
    """Which prompt produced a proposal. Part of the candidate's identity."""

    version: str
    sha256: str


@dataclass(frozen=True)
class Proposal:
    """One RawCandidate with everything needed to identify and place it."""

    id: str
    chunk: TranscriptChunk
    ordinal: int
    raw: RawCandidate


@dataclass(frozen=True)
class _Scored:
    """A validated, snapped, scored candidate, before any global decision."""

    id: str
    chunk_id: str
    raw: RawCandidate
    boundary: BoundaryAdjustment
    total_score: float

    @property
    def start(self) -> float:
        return self.boundary.adjusted_start

    @property
    def end(self) -> float:
        return self.boundary.adjusted_end

    @property
    def priority(self) -> tuple[float, float, str]:
        """The one ordering used by deduplication and by ranking alike.

        Highest total first, then the earlier interval, then the identifier.
        The identifier is a total order and is derived from the proposal before
        snapping, so two candidates can never tie all the way through and the
        result cannot depend on the order the proposals happened to arrive in.
        """
        return (-self.total_score, self.start, self.id)


def candidate_id(
    transcript_sha256: str,
    chunk_id: str,
    ordinal: int,
    proposed: RawCandidate,
    prompt: PromptIdentity,
) -> str:
    """A reproducible identifier, computed before the boundary is touched.

    Before, because the identifier is also the last tie-break in every ordering
    below. Deriving it from the adjusted interval would make it depend on the
    snapping that has not run yet, and deriving it from the content alone would
    give two identical proposals — the same moment returned by two overlapping
    chunks, or twice inside one batch — the same identifier, which the collection
    refuses and which would silently destroy one of the two records the funnel
    needs in order to count duplicates.

    ``chunk_id`` and ``ordinal`` are therefore in the payload alongside the
    proposal itself: the triple is unique by construction within a run.
    """
    payload = {
        "version": CANDIDATE_ID_VERSION,
        "transcript_sha256": transcript_sha256,
        "chunk_id": chunk_id,
        "ordinal": ordinal,
        "prompt_version": prompt.version,
        "prompt_sha256": prompt.sha256,
        "proposed": proposed.model_dump(mode="json"),
    }
    return f"cand_{canonical_sha256(payload)[:16]}"


# ---------------------------------------------------------------------------
# CE-030 - validation before anything is computed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Anchor:
    """A real point in the transcript a boundary may be moved onto."""

    value: float
    anchor: BoundaryAnchor
    #: 0 for a segment, 1 for a word. A segment edge is a stronger editorial
    #: boundary than a word edge, so it wins a tie on distance.
    rank: int


def _start_anchors(chunk: TranscriptChunk) -> list[_Anchor]:
    anchors = [
        _Anchor(segment.start, BoundaryAnchor.SEGMENT_START, 0) for segment in chunk.segments
    ]
    anchors.extend(
        _Anchor(word.start, BoundaryAnchor.WORD_START, 1)
        for segment in chunk.segments
        for word in segment.words
    )
    return anchors


def _end_anchors(chunk: TranscriptChunk) -> list[_Anchor]:
    anchors = [_Anchor(segment.end, BoundaryAnchor.SEGMENT_END, 0) for segment in chunk.segments]
    anchors.extend(
        _Anchor(word.end, BoundaryAnchor.WORD_END, 1)
        for segment in chunk.segments
        for word in segment.words
    )
    return anchors


def _spoken_intervals(chunk: TranscriptChunk) -> list[tuple[float, float]]:
    intervals = [(segment.start, segment.end) for segment in chunk.segments]
    intervals.extend((word.start, word.end) for segment in chunk.segments for word in segment.words)
    return intervals


def is_grounded(instant: float, chunk: TranscriptChunk, snap_seconds: float) -> bool:
    """Whether a timestamp is supported by the transcript of this chunk.

    The specification asks for timestamps "grounded near transcript content"
    without saying what near means, so this is the baseline it is formalised to,
    and it is deliberately arithmetic only — no text similarity, no heuristic, no
    model. An instant is grounded when either:

    - it falls inside a segment or word interval of this chunk, which covers a
      timestamp in the middle of a long sentence, far from both of its edges; or
    - it lies within ``boundary_snap_seconds`` of a real segment or word edge,
      which covers a timestamp in a pause and is exactly the reach CE-031 would
      have had if it were allowed to move it.

    Tying the tolerance to the snapping window is the substantive choice here.
    An endpoint the snapper could not have reached from is an endpoint nothing in
    the transcript supports, and accepting it would mean clipping audio the
    analyzer never actually saw a boundary for.

    A chunk with no word timestamps is still groundable through its segment
    edges, so ``word_timestamps = false`` degrades the precision of this rule
    rather than rejecting every candidate.
    """
    for start, end in _spoken_intervals(chunk):
        if start - TIME_EPSILON <= instant <= end + TIME_EPSILON:
            return True
    reach = snap_seconds + TIME_EPSILON
    edges = (*_start_anchors(chunk), *_end_anchors(chunk))
    return any(abs(instant - edge.value) <= reach for edge in edges)


def validate_proposal(
    proposed: RawCandidate,
    chunk: TranscriptChunk,
    source_duration_seconds: float,
    policy: CandidatePolicy,
) -> list[RejectionReason]:
    """Every reason this proposal cannot go forward, in canonical order.

    All applicable reasons are collected rather than the first one found. A
    proposal that is both inverted and outside its chunk says something
    different about the prompt than one that is merely inverted, and the whole
    purpose of keeping refused proposals is to be able to see that.

    Duration rules are skipped when there is no positive duration to judge:
    calling an inverted interval "too short" would be inventing a measurement of
    something that is not a length.
    """
    reasons: set[RejectionReason] = set()
    start, end = proposed.start, proposed.end

    if start < 0 or end <= start + TIME_EPSILON:
        reasons.add(RejectionReason.INVALID_INTERVAL)
    if start < chunk.start - TIME_EPSILON or end > chunk.end + TIME_EPSILON:
        reasons.add(RejectionReason.OUTSIDE_CHUNK)
    if end > source_duration_seconds + TIME_EPSILON:
        reasons.add(RejectionReason.END_BEYOND_SOURCE)

    duration = end - start
    if duration > TIME_EPSILON:
        if duration < policy.min_duration_seconds - TIME_EPSILON:
            reasons.add(RejectionReason.TOO_SHORT)
        if duration > policy.max_duration_seconds + TIME_EPSILON:
            reasons.add(RejectionReason.TOO_LONG)

    snap = policy.boundary_snap_seconds
    if not is_grounded(start, chunk, snap) or not is_grounded(end, chunk, snap):
        reasons.add(RejectionReason.UNGROUNDED)

    return [reason for reason in REASON_ORDER if reason in reasons]


# ---------------------------------------------------------------------------
# CE-031 - boundary snapping
# ---------------------------------------------------------------------------


def _nearest(
    instant: float, anchors: list[_Anchor], snap_seconds: float, prefer_earlier: bool
) -> _Anchor | None:
    """The anchor a boundary should move onto, or None to leave it alone.

    Ordering is total and explicit: nearest wins, a segment edge beats a word
    edge at the same distance, and a remaining tie is settled by taking the
    earliest for a start and the latest for an end — widening the clip rather
    than narrowing it, because a clip that starts a word early is watchable and
    one that starts a word late is not.
    """
    reach = snap_seconds + TIME_EPSILON
    within = [anchor for anchor in anchors if abs(anchor.value - instant) <= reach]
    if not within:
        return None
    direction = 1.0 if prefer_earlier else -1.0
    return min(
        within,
        key=lambda anchor: (abs(anchor.value - instant), anchor.rank, direction * anchor.value),
    )


def snap_boundary(
    proposed: RawCandidate,
    chunk: TranscriptChunk,
    source_duration_seconds: float,
    policy: CandidatePolicy,
) -> BoundaryAdjustment:
    """Move a validated interval onto natural speech boundaries, or leave it.

    Only intervals that passed CE-030 reach this function, so the proposal is
    always well formed and the adjustment can be recorded without any optional
    field.

    The whole adjustment is reverted as a unit when the result would leave the
    interval unusable — inverted, past the end of the source, or outside the
    duration policy. Reverting rather than discarding is the substantive rule:
    the analyzer's judgement was that this moment is worth clipping, and an
    editorial refinement that breaks it should lose to that judgement, not the
    other way round. ADR-010 asks for better boundaries, not for fewer
    candidates.
    """
    snap = policy.boundary_snap_seconds
    start_anchor = _nearest(proposed.start, _start_anchors(chunk), snap, prefer_earlier=True)
    end_anchor = _nearest(proposed.end, _end_anchors(chunk), snap, prefer_earlier=False)

    adjusted_start = start_anchor.value if start_anchor else proposed.start
    adjusted_end = end_anchor.value if end_anchor else proposed.end
    duration = adjusted_end - adjusted_start

    unusable = (
        duration <= TIME_EPSILON
        or adjusted_start < 0
        or adjusted_end > source_duration_seconds + TIME_EPSILON
        or duration < policy.min_duration_seconds - TIME_EPSILON
        or duration > policy.max_duration_seconds + TIME_EPSILON
    )
    if unusable:
        return BoundaryAdjustment(
            proposed_start=proposed.start,
            proposed_end=proposed.end,
            adjusted_start=proposed.start,
            adjusted_end=proposed.end,
            start_delta=0.0,
            end_delta=0.0,
            start_anchor=BoundaryAnchor.UNCHANGED,
            end_anchor=BoundaryAnchor.UNCHANGED,
            window_seconds=snap,
            reverted=True,
        )

    return BoundaryAdjustment(
        proposed_start=proposed.start,
        proposed_end=proposed.end,
        adjusted_start=adjusted_start,
        adjusted_end=adjusted_end,
        start_delta=adjusted_start - proposed.start,
        end_delta=adjusted_end - proposed.end,
        start_anchor=start_anchor.anchor if start_anchor else BoundaryAnchor.UNCHANGED,
        end_anchor=end_anchor.anchor if end_anchor else BoundaryAnchor.UNCHANGED,
        window_seconds=snap,
        reverted=False,
    )


# ---------------------------------------------------------------------------
# CE-032 - temporal IoU and global deduplication
# ---------------------------------------------------------------------------


def interval_iou(
    first_start: float, first_end: float, second_start: float, second_end: float
) -> float:
    """Intersection over union of two time intervals, on 0.0 to 1.0.

    Disjoint or merely touching intervals give 0.0; identical intervals give
    1.0. Both inputs are well-formed positive-length intervals here — they are
    adjusted boundaries of validated candidates — so the union is never zero and
    the division is always defined.

    ``CandidateCollection`` recomputes this independently from its own
    definition when it validates a DeduplicationEvent. The duplication is
    deliberate: sharing one helper would let a change in how deduplication
    decides silently redefine what its own audit trail claims.
    """
    overlap = min(first_end, second_end) - max(first_start, second_start)
    if overlap <= 0:
        return 0.0
    union = (first_end - first_start) + (second_end - second_start) - overlap
    return overlap / union


def deduplicate(
    scored: list[_Scored], threshold: float
) -> tuple[list[_Scored], list[tuple[_Scored, _Scored, float]]]:
    """Drop candidates covering a moment a better candidate already covers.

    Global, across every chunk, and run after the minimum-score filter so that a
    candidate below the threshold can never be the reason a good one is dropped.

    The strategy is greedy over the priority order, which makes it stable and
    makes the keeper well defined even when a candidate overlaps several of the
    ones already kept: the survivors are visited in priority order, so the first
    match is the highest-priority match. Greedy also means the relation does not
    have to be transitive — A can absorb B while C, which overlaps B but not A,
    survives on its own — and that is the correct outcome, because C really is
    a different moment from A.
    """
    kept: list[_Scored] = []
    events: list[tuple[_Scored, _Scored, float]] = []
    for candidate in sorted(scored, key=lambda item: item.priority):
        duplicate_of: _Scored | None = None
        overlap = 0.0
        for survivor in kept:
            ratio = interval_iou(survivor.start, survivor.end, candidate.start, candidate.end)
            if ratio >= threshold - IOU_EPSILON:
                duplicate_of = survivor
                overlap = ratio
                break
        if duplicate_of is None:
            kept.append(candidate)
        else:
            events.append((duplicate_of, candidate, overlap))
    return kept, events


# ---------------------------------------------------------------------------
# CE-033 - global ranking and the top-N ceiling
# ---------------------------------------------------------------------------


def _validated(
    scored: _Scored,
    status: CandidateStatus,
    reasons: list[RejectionReason],
    rank: int | None = None,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        id=scored.id,
        chunk_id=scored.chunk_id,
        rank=rank,
        start=scored.start,
        end=scored.end,
        duration=scored.end - scored.start,
        category=scored.raw.category,
        topic=scored.raw.topic,
        hook=scored.raw.hook,
        summary=scored.raw.summary,
        reason=scored.raw.reason,
        scores=scored.raw.scores,
        total_score=scored.total_score,
        score_formula_version=SCORE_FORMULA_VERSION,
        boundary=scored.boundary,
        status=status,
        rejection_reasons=reasons,
        warnings=list(scored.raw.warnings),
    )


def select_candidates(
    proposals: list[Proposal],
    source_duration_seconds: float,
    policy: CandidatePolicy,
    chunking_rules_version: int,
    generated_at: datetime,
) -> CandidateCollection:
    """Run the whole deterministic pipeline and build the validated collection.

    The result is independent of the order the proposals arrive in. Every
    comparison ends in the identifier, which is derived from the chunk, the
    ordinal within its batch and the proposal itself, so permuting the batches
    or the proposals inside them changes nothing as long as the same proposal
    keeps the same origin.
    """
    invalid: list[InvalidCandidate] = []
    scored: list[_Scored] = []

    # Canonical order first, so nothing below inherits the order the proposals
    # happened to arrive in. Everything downstream is sorted by priority, but the
    # invalid list is not — it has no score to sort by — and it would otherwise be
    # the one place where permuting the batches changed the artifact.
    ordered = sorted(proposals, key=lambda item: (item.chunk.id, item.ordinal))
    for proposal in ordered:
        reasons = validate_proposal(proposal.raw, proposal.chunk, source_duration_seconds, policy)
        if reasons:
            invalid.append(
                InvalidCandidate(
                    id=proposal.id,
                    chunk_id=proposal.chunk.id,
                    proposed=proposal.raw,
                    rejection_reasons=reasons,
                )
            )
            continue
        boundary = snap_boundary(proposal.raw, proposal.chunk, source_duration_seconds, policy)
        scored.append(
            _Scored(
                id=proposal.id,
                chunk_id=proposal.chunk.id,
                raw=proposal.raw,
                boundary=boundary,
                total_score=calculate_total(proposal.raw.scores),
            )
        )

    # A total exactly at the threshold survives: min_score is the lowest score
    # worth a human's attention, not the lowest score that is disqualifying.
    survivors = [item for item in scored if item.total_score + SCORE_EPSILON >= policy.min_score]
    weak = [item for item in scored if item.total_score + SCORE_EPSILON < policy.min_score]

    kept, dedupe_pairs = deduplicate(survivors, policy.dedupe_iou)
    ranked = sorted(kept, key=lambda item: item.priority)
    selected = ranked[: policy.max_candidates]
    cut = ranked[policy.max_candidates :]

    candidates = [
        _validated(item, CandidateStatus.SUGGESTED, [], rank=position)
        for position, item in enumerate(selected, start=1)
    ]
    rejected = [
        *(
            _validated(item, CandidateStatus.REJECTED, [RejectionReason.BELOW_MIN_SCORE])
            for item in sorted(weak, key=lambda item: item.priority)
        ),
        *(
            _validated(dropped, CandidateStatus.DEDUPLICATED, [RejectionReason.DUPLICATE])
            for _, dropped, _ in dedupe_pairs
        ),
        *(
            _validated(item, CandidateStatus.REJECTED, [RejectionReason.NOT_IN_TOP_N])
            for item in cut
        ),
    ]
    events = [
        DeduplicationEvent(
            kept_id=keeper.id,
            dropped_id=dropped.id,
            iou=overlap,
            kept_score=keeper.total_score,
            dropped_score=dropped.total_score,
        )
        for keeper, dropped, overlap in dedupe_pairs
    ]

    return CandidateCollection(
        schema_version=CANDIDATES_SCHEMA_VERSION,
        rules_version=chunking_rules_version,
        score_formula_version=SCORE_FORMULA_VERSION,
        generated_at=generated_at,
        source_duration_seconds=source_duration_seconds,
        target_candidates=policy.target_candidates,
        max_candidates=policy.max_candidates,
        min_score=policy.min_score,
        dedupe_iou=policy.dedupe_iou,
        boundary_snap_seconds=policy.boundary_snap_seconds,
        # Counted from the records themselves. Deriving one of these from a
        # subtraction would make the collection's own cross-checks tautological.
        counts=CandidateCounts(
            proposed=len(proposals),
            invalid=len(invalid),
            below_min_score=len(weak),
            deduplicated=len(dedupe_pairs),
            not_in_top_n=len(cut),
            selected=len(candidates),
        ),
        candidates=candidates,
        rejected=rejected,
        invalid=invalid,
        deduplication_events=events,
    )
