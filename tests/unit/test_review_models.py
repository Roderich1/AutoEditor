"""CE-036 to CE-039: what a single human decision may and may not say.

The rule these tests exist to hold is that a decision record cannot lie about
what a person did. An approval keeps the interval it approved, an edit has to be
an actual movement, and a rejection has no final interval at all because there
is none to have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from content_engine.domain.enums import EditorialReason, ReviewDecisionType
from content_engine.domain.review import (
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
)

REVIEWED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def approved(**overrides: Any) -> ApprovedDecision:
    payload: dict[str, Any] = {
        "candidate_id": "cand_0001",
        "original_start": 10.0,
        "original_end": 40.0,
        "final_start": 10.0,
        "final_end": 40.0,
        "reviewed_at": REVIEWED_AT,
    }
    payload.update(overrides)
    return ApprovedDecision(**payload)


def rejected(**overrides: Any) -> RejectedDecision:
    payload: dict[str, Any] = {
        "candidate_id": "cand_0001",
        "original_start": 10.0,
        "original_end": 40.0,
        "reviewed_at": REVIEWED_AT,
    }
    payload.update(overrides)
    return RejectedDecision(**payload)


def edited(**overrides: Any) -> EditedDecision:
    payload: dict[str, Any] = {
        "candidate_id": "cand_0001",
        "original_start": 10.0,
        "original_end": 40.0,
        "final_start": 12.0,
        "final_end": 38.0,
        "reviewed_at": REVIEWED_AT,
    }
    payload.update(overrides)
    return EditedDecision(**payload)


class TestDiscriminator:
    def test_each_kind_carries_its_own_literal(self) -> None:
        assert approved().decision is ReviewDecisionType.APPROVED
        assert rejected().decision is ReviewDecisionType.REJECTED
        assert edited().decision is ReviewDecisionType.EDITED

    def test_the_literal_cannot_be_overridden(self) -> None:
        with pytest.raises(ValidationError):
            approved(decision="rejected")

    @pytest.mark.parametrize("model", [ApprovedDecision, RejectedDecision, EditedDecision])
    def test_unknown_fields_are_refused(self, model: type) -> None:
        with pytest.raises(ValidationError):
            model(
                candidate_id="cand_0001",
                original_start=10.0,
                original_end=40.0,
                final_start=10.0,
                final_end=40.0,
                reviewed_at=REVIEWED_AT,
                verdict="lgtm",
            )


class TestApproved:
    def test_final_interval_is_the_original_one(self) -> None:
        assert approved().final_interval == (10.0, 40.0)

    @pytest.mark.parametrize(("field", "value"), [("final_start", 10.5), ("final_end", 39.0)])
    def test_moved_bounds_are_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError, match="approved"):
            approved(**{field: value})

    def test_it_carries_no_rejection_reason(self) -> None:
        assert not hasattr(approved(), "reason")


class TestRejected:
    def test_a_reason_is_optional(self) -> None:
        assert rejected().reason is None

    def test_it_has_no_final_interval_to_offer(self) -> None:
        assert rejected().final_interval is None

    def test_a_structured_reason_is_kept(self) -> None:
        assert rejected(reason=EditorialReason.WEAK_HOOK).reason is EditorialReason.WEAK_HOOK

    def test_other_demands_a_detail(self) -> None:
        with pytest.raises(ValidationError, match="other"):
            rejected(reason=EditorialReason.OTHER)

    @pytest.mark.parametrize("detail", ["", "   ", "\n"])
    def test_a_blank_detail_does_not_satisfy_other(self, detail: str) -> None:
        with pytest.raises(ValidationError, match="other"):
            rejected(reason=EditorialReason.OTHER, detail=detail)

    def test_other_with_a_detail_is_accepted(self) -> None:
        decision = rejected(reason=EditorialReason.OTHER, detail="the speaker misnames the flag")
        assert decision.detail == "the speaker misnames the flag"

    def test_a_detail_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="detail"):
            rejected(detail="no reason given")

    def test_a_detail_may_accompany_a_named_reason(self) -> None:
        decision = rejected(reason=EditorialReason.BAD_BOUNDARY, detail="starts mid-sentence")
        assert decision.detail == "starts mid-sentence"

    def test_an_unknown_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            rejected(reason="not_funny")


class TestEdited:
    def test_the_final_interval_is_the_edited_one(self) -> None:
        assert edited().final_interval == (12.0, 38.0)

    def test_an_edit_that_moves_nothing_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="differ"):
            edited(final_start=10.0, final_end=40.0)

    def test_moving_only_the_start_is_a_real_edit(self) -> None:
        assert edited(final_start=11.0, final_end=40.0).final_interval == (11.0, 40.0)

    def test_moving_only_the_end_is_a_real_edit(self) -> None:
        assert edited(final_start=10.0, final_end=41.0).final_interval == (10.0, 41.0)

    def test_an_inverted_final_interval_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            edited(final_start=38.0, final_end=12.0)

    def test_a_zero_length_final_interval_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            edited(final_start=20.0, final_end=20.0)

    def test_a_negative_start_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            edited(final_start=-1.0)

    def test_an_edit_may_be_shorter_than_the_analyzer_minimum(self) -> None:
        """CE-038. The duration policy constrains what the model may propose.

        A person watching the preview is the authority on where their clip ends,
        and silently widening that interval back to the AI minimum would render
        something they did not choose.
        """
        assert edited(final_start=12.0, final_end=14.0).final_interval == (12.0, 14.0)

    def test_an_edit_may_be_longer_than_the_analyzer_maximum(self) -> None:
        assert edited(final_start=10.0, final_end=400.0).final_interval == (10.0, 400.0)


class TestNonFiniteTimestamps:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    @pytest.mark.parametrize(
        "field", ["original_start", "original_end", "final_start", "final_end"]
    )
    def test_refused_on_an_edit(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            edited(**{field: value})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    @pytest.mark.parametrize("field", ["original_start", "original_end"])
    def test_refused_on_a_rejection(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            rejected(**{field: value})


class TestOriginalInterval:
    def test_an_inverted_original_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            approved(original_start=40.0, original_end=10.0, final_start=40.0, final_end=10.0)

    def test_a_blank_candidate_id_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            approved(candidate_id="")


class TestReviewedAt:
    def test_a_naive_timestamp_is_refused(self) -> None:
        naive = datetime(2026, 3, 1, 12, 0)  # noqa: DTZ001 - the point of the test
        with pytest.raises(ValidationError, match="UTC"):
            approved(reviewed_at=naive)

    def test_a_non_utc_timestamp_is_refused(self) -> None:
        elsewhere = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        with pytest.raises(ValidationError, match="UTC"):
            approved(reviewed_at=elsewhere)
