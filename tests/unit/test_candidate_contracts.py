"""Adversarial tests for the candidate contracts.

Every test here was written against the models as they stood after the first
foundation commit, and every one of them failed. They encode the invariants the
deterministic pipeline in the next pull request is entitled to assume, so that
CE-030 to CE-033 can be written against a contract rather than against hope.

The theme is that a record must never be able to lie about what happened to it:
a proposal refused before it was measured cannot carry an interval it never
earned, a funnel cannot report counts its own lists contradict, and an
adjustment cannot claim a delta it did not apply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from content_engine.domain.candidates import (
    BoundaryAdjustment,
    CandidateCollection,
    CandidateCounts,
    CandidateScores,
    DeduplicationEvent,
    InvalidCandidate,
    RawCandidate,
    TranscriptChunk,
    ValidatedCandidate,
)
from content_engine.domain.enums import (
    BoundaryAnchor,
    CandidateStatus,
    ClipCategory,
    RejectionReason,
)
from content_engine.domain.models import TranscriptSegment

SCORES = CandidateScores(
    hook=92,
    value=88,
    context_independence=96,
    clarity=93,
    engagement_potential=84,
    relevance=90,
)


def _raw(**overrides: Any) -> RawCandidate:
    payload: dict[str, Any] = {
        "start": 100.0,
        "end": 150.0,
        "category": ClipCategory.PROBLEM_SOLUTION,
        "topic": "tema",
        "hook": "gancho",
        "summary": "resumen",
        "reason": "motivo",
        "scores": SCORES,
    }
    payload.update(overrides)
    return RawCandidate(**payload)


def _boundary(**overrides: Any) -> BoundaryAdjustment:
    payload: dict[str, Any] = {
        "proposed_start": 100.0,
        "proposed_end": 150.0,
        "adjusted_start": 99.5,
        "adjusted_end": 151.0,
        "start_delta": -0.5,
        "end_delta": 1.0,
        "start_anchor": BoundaryAnchor.SEGMENT_START,
        "end_anchor": BoundaryAnchor.SEGMENT_END,
        "window_seconds": 2.5,
    }
    payload.update(overrides)
    return BoundaryAdjustment(**payload)


def _validated(**overrides: Any) -> ValidatedCandidate:
    boundary = overrides.pop("boundary", _boundary())
    payload: dict[str, Any] = {
        "id": "cand_000000000001",
        "chunk_id": "chunk_0000",
        "start": boundary.adjusted_start,
        "end": boundary.adjusted_end,
        "duration": boundary.adjusted_end - boundary.adjusted_start,
        "category": ClipCategory.PROBLEM_SOLUTION,
        "topic": "tema",
        "hook": "gancho",
        "summary": "resumen",
        "reason": "motivo",
        "scores": SCORES,
        "total_score": 91.15,
        "score_formula_version": 1,
        "boundary": boundary,
        "status": CandidateStatus.SUGGESTED,
    }
    payload.update(overrides)
    return ValidatedCandidate(**payload)


def _invalid(**overrides: Any) -> InvalidCandidate:
    payload: dict[str, Any] = {
        "id": "cand_ffffffffffff",
        "chunk_id": "chunk_0000",
        "proposed": _raw(),
        "rejection_reasons": [RejectionReason.TOO_LONG],
    }
    payload.update(overrides)
    return InvalidCandidate(**payload)


def _collection(**overrides: Any) -> CandidateCollection:
    payload: dict[str, Any] = {
        "rules_version": 1,
        "score_formula_version": 1,
        "generated_at": datetime.now(UTC),
        "source_duration_seconds": 1200.0,
        "target_candidates": 10,
        "max_candidates": 15,
        "min_score": 65.0,
        "dedupe_iou": 0.6,
        "boundary_snap_seconds": 2.5,
        "counts": CandidateCounts(
            proposed=1,
            invalid=0,
            below_min_score=0,
            deduplicated=0,
            not_in_top_n=0,
            selected=1,
        ),
        "candidates": [_validated(rank=1)],
        "rejected": [],
        "invalid": [],
        "deduplication_events": [],
    }
    payload.update(overrides)
    return CandidateCollection(**payload)


# ---------------------------------------------------------------------------
# 2. Provider output that is temporally impossible must survive to be diagnosed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(-5.0, 50.0, id="negative-start"),
        pytest.param(0.0, 0.0, id="zero-length"),
        pytest.param(100.0, 50.0, id="inverted"),
        pytest.param(-10.0, -5.0, id="entirely-negative"),
    ],
)
def test_a_temporally_impossible_proposal_is_preserved_not_refused(
    start: float, end: float
) -> None:
    """CE-030 owns interval rules, so the model must let the proposal through.

    Refusing it here would turn a measurable failure mode of the prompt into a
    parse error with nothing recorded, and the rate at which a model invents
    impossible timestamps is one of the things this project exists to measure.
    """
    candidate = _raw(start=start, end=end)

    assert candidate.start == start
    assert candidate.end == end


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="-inf"),
    ],
)
@pytest.mark.parametrize("field", ["start", "end"])
def test_a_non_finite_timestamp_is_still_refused(field: str, value: float) -> None:
    """Impossible is recorded; not-a-number is refused. They are different."""
    with pytest.raises(ValidationError):
        _raw(**{field: value})


def test_an_impossible_proposal_is_never_silently_repaired() -> None:
    candidate = _raw(start=-5.0, end=-1.0)

    assert candidate.start != 0.0
    assert candidate.end != 0.0


def test_a_candidate_refused_before_snapping_has_no_interval_of_its_own() -> None:
    """It never earned one. Inventing one to fill a field would be a lie."""
    rejected = _invalid(
        proposed=_raw(start=100.0, end=50.0),
        rejection_reasons=[RejectionReason.INVALID_INTERVAL],
    )

    assert not hasattr(rejected, "start")
    assert not hasattr(rejected, "end")
    assert not hasattr(rejected, "duration")
    assert not hasattr(rejected, "boundary")
    assert rejected.proposed.start == 100.0
    assert rejected.proposed.end == 50.0


def test_an_invalid_candidate_must_say_why_it_was_refused() -> None:
    with pytest.raises(ValidationError):
        _invalid(rejection_reasons=[])


# ---------------------------------------------------------------------------
# 3. BoundaryAdjustment cannot claim an adjustment it did not make
# ---------------------------------------------------------------------------


def test_a_start_delta_must_equal_the_movement_it_describes() -> None:
    with pytest.raises(ValidationError, match="start_delta"):
        _boundary(start_delta=-99.0)


def test_an_end_delta_must_equal_the_movement_it_describes() -> None:
    with pytest.raises(ValidationError, match="end_delta"):
        _boundary(end_delta=99.0)


def test_an_adjusted_interval_cannot_be_inverted() -> None:
    with pytest.raises(ValidationError, match="adjusted"):
        _boundary(adjusted_start=200.0, adjusted_end=100.0, start_delta=100.0, end_delta=-50.0)


def test_a_proposed_interval_reaching_snapping_cannot_be_inverted() -> None:
    """Only intervals that passed CE-030 are snapped, so this cannot happen."""
    with pytest.raises(ValidationError, match="proposed"):
        _boundary(
            proposed_start=150.0,
            proposed_end=100.0,
            adjusted_start=150.0,
            adjusted_end=160.0,
            start_delta=0.0,
            end_delta=60.0,
        )


def test_a_reverted_adjustment_must_actually_restore_the_proposal() -> None:
    with pytest.raises(ValidationError, match="reverted"):
        _boundary(reverted=True)


def test_a_reverted_adjustment_cannot_keep_a_non_zero_delta() -> None:
    with pytest.raises(ValidationError, match="reverted"):
        _boundary(
            adjusted_start=100.0,
            adjusted_end=150.0,
            start_delta=0.5,
            end_delta=0.0,
            reverted=True,
        )


def test_a_reverted_adjustment_reports_that_no_anchor_was_used() -> None:
    with pytest.raises(ValidationError, match="reverted"):
        _boundary(
            adjusted_start=100.0,
            adjusted_end=150.0,
            start_delta=0.0,
            end_delta=0.0,
            start_anchor=BoundaryAnchor.SEGMENT_START,
            end_anchor=BoundaryAnchor.UNCHANGED,
            reverted=True,
        )


def test_a_correctly_reverted_adjustment_is_accepted() -> None:
    boundary = _boundary(
        adjusted_start=100.0,
        adjusted_end=150.0,
        start_delta=0.0,
        end_delta=0.0,
        start_anchor=BoundaryAnchor.UNCHANGED,
        end_anchor=BoundaryAnchor.UNCHANGED,
        reverted=True,
    )

    assert boundary.adjusted_start == boundary.proposed_start
    assert boundary.adjusted_end == boundary.proposed_end


def test_float_noise_in_a_delta_is_tolerated() -> None:
    """0.1 + 0.2 is not 0.3; the check must not fail on representation error."""
    boundary = _boundary(
        proposed_start=0.1,
        proposed_end=150.0,
        adjusted_start=0.3,
        adjusted_end=150.0,
        start_delta=0.1 + 0.1,
        end_delta=0.0,
        end_anchor=BoundaryAnchor.UNCHANGED,
    )

    assert boundary.start_delta == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# 4. A validated candidate must agree with its own adjustment
# ---------------------------------------------------------------------------


def test_a_candidate_interval_must_match_its_adjusted_boundary() -> None:
    with pytest.raises(ValidationError, match="adjusted_start"):
        _validated(start=1.0, end=151.0, duration=150.0)


def test_a_candidate_end_must_match_its_adjusted_boundary() -> None:
    with pytest.raises(ValidationError, match="adjusted_end"):
        _validated(end=999.0, duration=899.5)


def test_a_candidate_duration_must_match_its_own_interval() -> None:
    with pytest.raises(ValidationError, match="declares duration"):
        _validated(duration=10.0)


# ---------------------------------------------------------------------------
# 5. The funnel cannot report counts its own lists contradict
# ---------------------------------------------------------------------------


def test_a_suggested_candidate_cannot_appear_in_the_rejected_list() -> None:
    with pytest.raises(ValidationError, match="rejected"):
        _collection(
            rejected=[_validated(id="cand_000000000002")],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=1,
                deduplicated=0,
                not_in_top_n=0,
                selected=1,
            ),
        )


def test_an_identifier_cannot_appear_twice() -> None:
    with pytest.raises(ValidationError, match="appears more than once"):
        _collection(
            candidates=[_validated(rank=1), _validated(rank=2)],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=0,
                deduplicated=0,
                not_in_top_n=0,
                selected=2,
            ),
        )


def test_an_identifier_cannot_be_in_both_candidates_and_rejected() -> None:
    dropped = _validated(
        status=CandidateStatus.DEDUPLICATED,
        rejection_reasons=[RejectionReason.DUPLICATE],
    )

    with pytest.raises(ValidationError, match="appears more than once"):
        _collection(
            rejected=[dropped],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=0,
                deduplicated=1,
                not_in_top_n=0,
                selected=1,
            ),
            deduplication_events=[
                DeduplicationEvent(
                    kept_id="cand_000000000001",
                    dropped_id="cand_000000000001",
                    iou=0.9,
                    kept_score=91.15,
                    dropped_score=80.0,
                )
            ],
        )


def test_the_selected_count_must_match_the_selected_list() -> None:
    with pytest.raises(ValidationError, match="selected"):
        _collection(
            counts=CandidateCounts(
                proposed=1,
                invalid=0,
                below_min_score=0,
                deduplicated=0,
                not_in_top_n=0,
                selected=7,
            )
        )


def test_the_invalid_count_must_match_the_invalid_list() -> None:
    with pytest.raises(ValidationError, match="invalid"):
        _collection(
            invalid=[_invalid()],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=0,
                deduplicated=0,
                not_in_top_n=0,
                selected=1,
            ),
        )


def test_the_proposed_count_must_account_for_every_outcome() -> None:
    """Each proposal ends in exactly one terminal state, so the total is a sum."""
    with pytest.raises(ValidationError, match="proposed"):
        _collection(
            counts=CandidateCounts(
                proposed=99,
                invalid=0,
                below_min_score=0,
                deduplicated=0,
                not_in_top_n=0,
                selected=1,
            )
        )


def test_a_deduplication_event_cannot_name_a_candidate_that_does_not_exist() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        _collection(
            deduplication_events=[
                DeduplicationEvent(
                    kept_id="cand_000000000001",
                    dropped_id="cand_does_not_exist",
                    iou=0.9,
                    kept_score=91.15,
                    dropped_score=80.0,
                )
            ],
            counts=CandidateCounts(
                proposed=1,
                invalid=0,
                below_min_score=0,
                deduplicated=1,
                not_in_top_n=0,
                selected=1,
            ),
        )


def test_the_deduplicated_count_must_match_the_recorded_events() -> None:
    dropped = _validated(
        id="cand_000000000002",
        status=CandidateStatus.DEDUPLICATED,
        rejection_reasons=[RejectionReason.DUPLICATE],
    )

    with pytest.raises(ValidationError, match="deduplicated"):
        _collection(
            rejected=[dropped],
            deduplication_events=[],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=0,
                deduplicated=1,
                not_in_top_n=0,
                selected=1,
            ),
        )


def test_a_coherent_funnel_is_accepted() -> None:
    dropped = _validated(
        id="cand_000000000002",
        status=CandidateStatus.DEDUPLICATED,
        rejection_reasons=[RejectionReason.DUPLICATE],
    )
    weak = _validated(
        id="cand_000000000003",
        total_score=10.0,
        status=CandidateStatus.REJECTED,
        rejection_reasons=[RejectionReason.BELOW_MIN_SCORE],
    )

    collection = _collection(
        rejected=[dropped, weak],
        invalid=[_invalid()],
        deduplication_events=[
            DeduplicationEvent(
                kept_id="cand_000000000001",
                dropped_id="cand_000000000002",
                iou=0.9,
                kept_score=91.15,
                dropped_score=91.15,
            )
        ],
        counts=CandidateCounts(
            proposed=4,
            invalid=1,
            below_min_score=1,
            deduplicated=1,
            not_in_top_n=0,
            selected=1,
        ),
    )

    assert collection.counts.proposed == 4
    assert len(collection.rejected) == 2
    assert len(collection.invalid) == 1


# ---------------------------------------------------------------------------
# 6. A chunk cannot misdescribe the segments it holds
# ---------------------------------------------------------------------------


def _segment(index: int, start: float, end: float) -> TranscriptSegment:
    return TranscriptSegment(index=index, start=start, end=end, text="hola", words=[])


def _chunk(**overrides: Any) -> TranscriptChunk:
    segments = overrides.pop("segments", [_segment(0, 0.0, 10.0), _segment(1, 10.0, 20.0)])
    payload: dict[str, Any] = {
        "id": "chunk_0000",
        "index": 0,
        "window_start": 0.0,
        "window_end": 360.0,
        "start": min(segment.start for segment in segments),
        "end": max(segment.end for segment in segments),
        "segment_indices": [segment.index for segment in segments],
        "segments": segments,
        "text": "x",
    }
    payload.update(overrides)
    return TranscriptChunk(**payload)


def test_a_chunk_index_must_match_the_segment_it_points_at() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        _chunk(segment_indices=[7, 1])


def test_chunk_indices_must_be_strictly_increasing() -> None:
    segments = [_segment(1, 10.0, 20.0), _segment(0, 0.0, 10.0)]

    with pytest.raises(ValidationError, match="increasing"):
        _chunk(segments=segments, segment_indices=[1, 0])


def test_chunk_indices_cannot_repeat() -> None:
    segments = [_segment(0, 0.0, 10.0), _segment(0, 0.0, 10.0)]

    with pytest.raises(ValidationError, match="increasing"):
        _chunk(segments=segments, segment_indices=[0, 0])


def test_chunk_segments_must_be_in_temporal_order() -> None:
    segments = [_segment(0, 50.0, 60.0), _segment(1, 10.0, 20.0)]

    with pytest.raises(ValidationError, match="temporal order"):
        _chunk(segments=segments)


def test_a_well_formed_chunk_is_accepted() -> None:
    chunk = _chunk()

    assert chunk.segment_indices == [0, 1]
    assert chunk.start == 0.0
    assert chunk.end == 20.0


def test_post_scoring_drops_must_match_the_rejected_list() -> None:
    """below_min_score, deduplicated and not_in_top_n are exactly `rejected`.

    A count claiming a candidate was scored and dropped, with no record of it in
    the list, would make the funnel unauditable in the direction that matters:
    something disappeared and nothing says which.
    """
    with pytest.raises(ValidationError, match="dropped after scoring"):
        _collection(
            rejected=[],
            counts=CandidateCounts(
                proposed=2,
                invalid=0,
                below_min_score=1,
                deduplicated=0,
                not_in_top_n=0,
                selected=1,
            ),
        )


def test_a_candidate_cut_by_the_cap_is_recorded_rather_than_dropped() -> None:
    """not_in_top_n exists so the cap is auditable, not silent."""
    cut = _validated(
        id="cand_000000000009",
        status=CandidateStatus.REJECTED,
        rejection_reasons=[RejectionReason.NOT_IN_TOP_N],
    )

    collection = _collection(
        max_candidates=1,
        rejected=[cut],
        counts=CandidateCounts(
            proposed=2,
            invalid=0,
            below_min_score=0,
            deduplicated=0,
            not_in_top_n=1,
            selected=1,
        ),
    )

    assert collection.counts.not_in_top_n == 1
    assert collection.rejected[0].total_score == 91.15
