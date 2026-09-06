"""Candidate domain schemas (CE-024).

The contract these models exist to enforce is that a language model may assert
six ratings and an interval, and nothing else. Everything here that looks like
paranoia is guarding that line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    BoundaryAdjustment,
    CandidateCollection,
    CandidateCounts,
    CandidateScores,
    DeduplicationEvent,
    RawCandidate,
    ValidatedCandidate,
)
from content_engine.domain.enums import (
    BoundaryAnchor,
    CandidateStatus,
    ClipCategory,
    RejectionReason,
)

SCORES = CandidateScores(
    hook=92,
    value=88,
    context_independence=96,
    clarity=93,
    engagement_potential=84,
    relevance=90,
)
DIMENSIONS = [
    "hook",
    "value",
    "context_independence",
    "clarity",
    "engagement_potential",
    "relevance",
]


def _raw(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start": 754.2,
        "end": 802.8,
        "category": ClipCategory.PROBLEM_SOLUTION,
        "topic": "Docker networking",
        "hook": "Este error de Docker me tomo una hora",
        "summary": "Explica un problema real de networking y su solucion.",
        "reason": "Problema identificable seguido de solucion concreta.",
        "scores": SCORES,
    }
    payload.update(overrides)
    return payload


def _boundary(**overrides: Any) -> BoundaryAdjustment:
    payload: dict[str, Any] = {
        "proposed_start": 754.2,
        "proposed_end": 802.8,
        "adjusted_start": 754.0,
        "adjusted_end": 803.0,
        "start_delta": -0.2,
        "end_delta": 0.2,
        "start_anchor": BoundaryAnchor.SEGMENT_START,
        "end_anchor": BoundaryAnchor.SEGMENT_END,
        "window_seconds": 2.5,
    }
    payload.update(overrides)
    return BoundaryAdjustment(**payload)


def _validated(**overrides: Any) -> ValidatedCandidate:
    payload: dict[str, Any] = {
        "id": "cand_0123456789ab",
        "chunk_id": "chunk_0000",
        "start": 754.0,
        "end": 803.0,
        "duration": 49.0,
        "category": ClipCategory.PROBLEM_SOLUTION,
        "topic": "Docker networking",
        "hook": "hook",
        "summary": "summary",
        "reason": "reason",
        "scores": SCORES,
        "total_score": 91.15,
        "score_formula_version": 1,
        "boundary": _boundary(),
        "status": CandidateStatus.SUGGESTED,
    }
    payload.update(overrides)
    return ValidatedCandidate(**payload)


# --- the line between what the model asserts and what code decides -----------


def test_the_scores_model_has_no_total_field() -> None:
    """ADR-008: the model rates dimensions; Python owns the arithmetic."""
    assert "total" not in CandidateScores.model_fields
    assert "total_score" not in CandidateScores.model_fields


def test_a_total_returned_by_a_provider_is_refused_loudly() -> None:
    """Ignoring it silently would hide a provider that ignores the schema."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CandidateScores(**{**SCORES.model_dump(), "total_score": 99.0})


def test_a_raw_candidate_refuses_unknown_fields() -> None:
    payload = _raw(total_score=99.0)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RawCandidate(**payload)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("value", [-1, 101])
def test_every_dimension_is_bounded_to_zero_through_one_hundred(dimension: str, value: int) -> None:
    payload = {**SCORES.model_dump(), dimension: value}

    with pytest.raises(ValidationError):
        CandidateScores(**payload)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="-inf"),
    ],
)
def test_no_dimension_accepts_a_non_finite_value(dimension: str, value: float) -> None:
    payload = {**SCORES.model_dump(), dimension: value}

    with pytest.raises(ValidationError):
        CandidateScores(**payload)


def test_the_six_dimensions_are_exactly_the_ones_the_formula_weights() -> None:
    assert set(CandidateScores.model_fields) == set(DIMENSIONS)


# --- raw candidates ----------------------------------------------------------


def test_a_raw_candidate_accepts_a_well_formed_proposal() -> None:
    candidate = RawCandidate(**_raw())

    assert candidate.category is ClipCategory.PROBLEM_SOLUTION
    assert candidate.warnings == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", -1.0),
        ("end", 0.0),
        ("topic", ""),
        ("hook", ""),
        ("summary", ""),
        ("reason", ""),
    ],
)
def test_a_raw_candidate_refuses_impossible_fields(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        RawCandidate(**_raw(**{field: value}))


def test_a_raw_candidate_refuses_an_invented_category() -> None:
    with pytest.raises(ValidationError):
        RawCandidate(**_raw(category="viral_moment"))


def test_an_inverted_interval_is_not_rejected_by_the_raw_model() -> None:
    """Raw output is untrusted, not pre-validated: CE-030 owns interval rules.

    The model only refuses what cannot be a number at all, so the rejection
    reason a candidate earns is recorded rather than lost to a parse error.
    """
    candidate = RawCandidate(**_raw(start=100.0, end=50.0))

    assert candidate.start > candidate.end


# --- validated candidates ----------------------------------------------------


def test_a_validated_candidate_must_agree_with_its_own_duration() -> None:
    with pytest.raises(ValidationError, match="declares duration"):
        _validated(duration=10.0)


def test_a_validated_candidate_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="at or before its start"):
        _validated(start=100.0, end=50.0, duration=50.0)


def test_a_suggested_candidate_cannot_carry_a_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="suggested but carries rejection"):
        _validated(rejection_reasons=[RejectionReason.TOO_SHORT])


def test_a_rejected_candidate_must_say_why() -> None:
    with pytest.raises(ValidationError, match="rejected without a reason"):
        _validated(status=CandidateStatus.REJECTED)


def test_a_candidate_that_was_not_selected_cannot_be_ranked() -> None:
    with pytest.raises(ValidationError, match="ranked but was not selected"):
        _validated(
            status=CandidateStatus.DEDUPLICATED,
            rank=1,
        )


def test_a_rejected_candidate_is_kept_with_its_reasons() -> None:
    candidate = _validated(
        status=CandidateStatus.REJECTED,
        rejection_reasons=[RejectionReason.TOO_LONG, RejectionReason.UNGROUNDED],
    )

    assert candidate.rejection_reasons == [
        RejectionReason.TOO_LONG,
        RejectionReason.UNGROUNDED,
    ]


# --- boundary adjustment -----------------------------------------------------


def test_the_boundary_record_keeps_the_proposal_beside_the_adjustment() -> None:
    """Without both, "correct idea, bad boundary" cannot be measured."""
    boundary = _boundary()

    assert boundary.proposed_start != boundary.adjusted_start
    assert boundary.start_delta == pytest.approx(-0.2)
    assert boundary.reverted is False


def test_a_reverted_adjustment_is_recorded_as_such() -> None:
    boundary = _boundary(
        adjusted_start=754.2,
        adjusted_end=802.8,
        start_delta=0.0,
        end_delta=0.0,
        start_anchor=BoundaryAnchor.UNCHANGED,
        end_anchor=BoundaryAnchor.UNCHANGED,
        reverted=True,
    )

    assert boundary.reverted
    assert boundary.adjusted_start == boundary.proposed_start


# --- collection --------------------------------------------------------------


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
            proposed=3, invalid=1, below_min_score=0, deduplicated=0, selected=2
        ),
        "candidates": [
            _validated(id="cand_a", rank=1),
            _validated(id="cand_b", rank=2),
        ],
        "rejected": [],
        "deduplication_events": [],
    }
    payload.update(overrides)
    return CandidateCollection(**payload)


def test_the_collection_declares_its_schema_version() -> None:
    assert _collection().schema_version == CANDIDATES_SCHEMA_VERSION


def test_ranks_must_be_contiguous_from_one() -> None:
    with pytest.raises(ValidationError, match="not contiguous"):
        _collection(candidates=[_validated(id="cand_a", rank=2)])


def test_the_hard_cap_is_enforced_by_the_model_itself() -> None:
    """max_candidates is a ceiling, not a suggestion: D-1."""
    too_many = [_validated(id=f"cand_{i}", rank=i + 1) for i in range(4)]

    with pytest.raises(ValidationError, match="exceed the hard cap"):
        _collection(max_candidates=3, candidates=too_many)


def test_the_target_may_differ_from_the_cap() -> None:
    """D-1: the target is the experimental objective, the cap is the limit."""
    collection = _collection(target_candidates=10, max_candidates=15)

    assert collection.target_candidates == 10
    assert collection.max_candidates == 15


def test_a_ranked_candidate_is_necessarily_suggested() -> None:
    """The collection needs no check of its own: the candidate model has it.

    A rank on anything not selected is already refused one level down, so the
    invariant holds by construction rather than by a second, weaker guard.
    """
    with pytest.raises(ValidationError, match="ranked but was not selected"):
        _validated(
            rank=1,
            status=CandidateStatus.REJECTED,
            rejection_reasons=[RejectionReason.TOO_SHORT],
        )


def test_the_collection_round_trips_through_json() -> None:
    collection = _collection()

    restored = CandidateCollection.model_validate(collection.model_dump(mode="json"))

    assert restored == collection


def test_a_deduplication_event_records_both_sides() -> None:
    event = DeduplicationEvent(
        kept_id="cand_a",
        dropped_id="cand_b",
        iou=0.72,
        kept_score=91.15,
        dropped_score=80.0,
    )

    assert event.kept_score > event.dropped_score


@pytest.mark.parametrize("iou", [-0.01, 1.01])
def test_an_iou_outside_zero_to_one_is_refused(iou: float) -> None:
    with pytest.raises(ValidationError):
        DeduplicationEvent(
            kept_id="a", dropped_id="b", iou=iou, kept_score=90.0, dropped_score=80.0
        )
