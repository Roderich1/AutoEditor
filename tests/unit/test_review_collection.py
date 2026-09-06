"""CE-039: the persisted collection of decisions, and what it refuses to be.

A decision is only meaningful against the exact shortlist it was taken over.
These tests hold the two halves of that: the collection's own invariants, and
the coherence check that ties it to one analysis and one set of candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from content_engine.domain.candidates import CandidateCollection, ValidatedCandidate
from content_engine.domain.enums import EditorialReason
from content_engine.domain.exceptions import IncompatibleArtifactError
from content_engine.domain.review import (
    DECISIONS_SCHEMA_VERSION,
    REVIEW_RULES_VERSION,
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
    ReviewDecisionCollection,
    decisions_coherence_problem,
    pending_candidates,
)
from content_engine.services.review_service import (
    DECISIONS_FILENAME,
    read_decisions,
    write_decisions,
)
from content_engine.utils.json import write_json
from tests.conftest import (
    chunk_of,
    collect,
    raw_candidate,
    speech_transcript,
    weak_candidate,
)

FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
SOURCE_DURATION = 119.0
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def shortlist() -> CandidateCollection:
    """Two selected candidates, both comfortably inside the source."""
    return collect(
        chunk_of(speech_transcript()),
        [raw_candidate(10.0, 39.0), raw_candidate(60.0, 89.0, hook=88)],
    )


def selected() -> list[ValidatedCandidate]:
    return list(shortlist().candidates)


def approval_of(candidate: ValidatedCandidate) -> ApprovedDecision:
    return ApprovedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        final_start=candidate.start,
        final_end=candidate.end,
        reviewed_at=NOW,
    )


def collection_of(*decisions: Any, **overrides: Any) -> ReviewDecisionCollection:
    payload: dict[str, Any] = {
        "analysis_fingerprint": FINGERPRINT,
        "source_duration_seconds": SOURCE_DURATION,
        "created_at": NOW,
        "updated_at": NOW,
        "decisions": list(decisions),
    }
    payload.update(overrides)
    return ReviewDecisionCollection(**payload)


class TestCollectionInvariants:
    def test_an_empty_collection_is_valid(self) -> None:
        collection = collection_of()
        assert collection.decisions == []
        assert collection.schema_version == DECISIONS_SCHEMA_VERSION
        assert collection.rules_version == REVIEW_RULES_VERSION

    def test_a_repeated_candidate_is_refused(self) -> None:
        decision = approval_of(selected()[0])
        with pytest.raises(ValidationError, match="more than once"):
            collection_of(decision, decision)

    def test_an_edit_beyond_the_source_is_refused(self) -> None:
        candidate = selected()[0]
        with pytest.raises(ValidationError, match="source"):
            collection_of(
                EditedDecision(
                    candidate_id=candidate.id,
                    original_start=candidate.start,
                    original_end=candidate.end,
                    final_start=candidate.start,
                    final_end=SOURCE_DURATION + 1.0,
                    reviewed_at=NOW,
                )
            )

    def test_an_edit_ending_exactly_at_the_source_duration_is_allowed(self) -> None:
        candidate = selected()[0]
        collection = collection_of(
            EditedDecision(
                candidate_id=candidate.id,
                original_start=candidate.start,
                original_end=candidate.end,
                final_start=candidate.start,
                final_end=SOURCE_DURATION,
                reviewed_at=NOW,
            )
        )
        assert collection.decisions[0].final_interval == (candidate.start, SOURCE_DURATION)

    def test_an_update_before_creation_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="updated_at"):
            collection_of(updated_at=NOW.replace(hour=11))

    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            collection_of(reviewer="andres")

    def test_decisions_are_indexed_by_candidate(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[0]))
        assert set(collection.by_candidate) == {candidates[0].id}

    def test_a_mixed_collection_reports_its_kinds(self) -> None:
        candidates = selected()
        collection = collection_of(
            approval_of(candidates[0]),
            RejectedDecision(
                candidate_id=candidates[1].id,
                original_start=candidates[1].start,
                original_end=candidates[1].end,
                reason=EditorialReason.WEAK_HOOK,
                reviewed_at=NOW,
            ),
        )
        assert collection.counts == {"approved": 1, "rejected": 1, "edited": 0}


class TestCoherenceWithTheShortlist:
    def test_a_matching_collection_has_no_problem(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[0]))
        assert (
            decisions_coherence_problem(collection, candidates, FINGERPRINT, SOURCE_DURATION)
            is None
        )

    def test_another_analysis_is_refused(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[0]))
        problem = decisions_coherence_problem(
            collection, candidates, OTHER_FINGERPRINT, SOURCE_DURATION
        )
        assert problem is not None
        assert "analysis" in problem

    def test_a_different_source_duration_is_refused(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[0]))
        problem = decisions_coherence_problem(collection, candidates, FINGERPRINT, 200.0)
        assert problem is not None
        assert "duration" in problem

    def test_a_decision_for_an_unknown_candidate_is_refused(self) -> None:
        candidates = selected()
        collection = collection_of(
            ApprovedDecision(
                candidate_id="cand_ghost",
                original_start=10.0,
                original_end=39.0,
                final_start=10.0,
                final_end=39.0,
                reviewed_at=NOW,
            )
        )
        problem = decisions_coherence_problem(collection, candidates, FINGERPRINT, SOURCE_DURATION)
        assert problem is not None
        assert "cand_ghost" in problem

    def test_a_decision_for_a_candidate_that_was_not_selected_is_refused(self) -> None:
        """Rejected, deduplicated and beyond-the-cap candidates are never shown."""
        full = collect(
            chunk_of(speech_transcript()),
            [raw_candidate(10.0, 39.0), weak_candidate(60.0, 89.0)],
        )
        assert full.rejected, "the fixture must produce something the pipeline dropped"
        dropped = full.rejected[0]
        collection = collection_of(
            ApprovedDecision(
                candidate_id=dropped.id,
                original_start=dropped.start,
                original_end=dropped.end,
                final_start=dropped.start,
                final_end=dropped.end,
                reviewed_at=NOW,
            )
        )
        problem = decisions_coherence_problem(
            collection, list(full.candidates), FINGERPRINT, SOURCE_DURATION
        )
        assert problem is not None
        assert dropped.id in problem

    @pytest.mark.parametrize("field", ["original_start", "original_end"])
    def test_manipulated_original_bounds_are_refused(self, field: str) -> None:
        candidates = selected()
        payload: dict[str, Any] = {
            "candidate_id": candidates[0].id,
            "original_start": candidates[0].start,
            "original_end": candidates[0].end,
            "final_start": candidates[0].start,
            "final_end": candidates[0].end,
            "reviewed_at": NOW,
        }
        payload[field] = payload[field] + 5.0
        payload["final_start"] = payload["original_start"]
        payload["final_end"] = payload["original_end"]
        collection = collection_of(ApprovedDecision(**payload))
        problem = decisions_coherence_problem(collection, candidates, FINGERPRINT, SOURCE_DURATION)
        assert problem is not None
        assert field in problem


class TestPending:
    def test_everything_is_pending_before_a_session(self) -> None:
        candidates = selected()
        assert pending_candidates(candidates, collection_of()) == candidates

    def test_a_decided_candidate_is_not_pending(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[0]))
        assert pending_candidates(candidates, collection) == [candidates[1]]

    def test_nothing_is_pending_once_every_candidate_is_decided(self) -> None:
        candidates = selected()
        collection = collection_of(*(approval_of(candidate) for candidate in candidates))
        assert pending_candidates(candidates, collection) == []

    def test_pending_keeps_rank_order(self) -> None:
        candidates = selected()
        collection = collection_of(approval_of(candidates[1]))
        assert [candidate.rank for candidate in pending_candidates(candidates, collection)] == [1]


class TestPersistence:
    def test_a_round_trip_preserves_every_decision(self, tmp_path: Path) -> None:
        candidates = selected()
        collection = collection_of(
            approval_of(candidates[0]),
            EditedDecision(
                candidate_id=candidates[1].id,
                original_start=candidates[1].start,
                original_end=candidates[1].end,
                final_start=candidates[1].start + 1.0,
                final_end=candidates[1].end,
                reviewed_at=NOW,
            ),
        )
        write_decisions(tmp_path, collection)
        assert read_decisions(tmp_path) == collection

    def test_the_file_is_written_atomically(self, tmp_path: Path) -> None:
        write_decisions(tmp_path, collection_of())
        assert [path.name for path in sorted(tmp_path.iterdir())] == [DECISIONS_FILENAME]

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(IncompatibleArtifactError, match=DECISIONS_FILENAME):
            read_decisions(tmp_path)

    def test_corrupt_json_is_refused(self, tmp_path: Path) -> None:
        tmp_path.joinpath(DECISIONS_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(IncompatibleArtifactError):
            read_decisions(tmp_path)

    def test_a_json_array_is_refused(self, tmp_path: Path) -> None:
        write_json(tmp_path.joinpath(DECISIONS_FILENAME), [])
        with pytest.raises(IncompatibleArtifactError):
            read_decisions(tmp_path)

    def test_an_unknown_schema_version_is_refused(self, tmp_path: Path) -> None:
        payload = collection_of().model_dump(mode="json")
        payload["schema_version"] = DECISIONS_SCHEMA_VERSION + 1
        write_json(tmp_path.joinpath(DECISIONS_FILENAME), payload)
        with pytest.raises(IncompatibleArtifactError, match="schema"):
            read_decisions(tmp_path)

    def test_an_unknown_decision_kind_is_refused(self, tmp_path: Path) -> None:
        payload = collection_of(approval_of(selected()[0])).model_dump(mode="json")
        payload["decisions"][0]["decision"] = "maybe"
        write_json(tmp_path.joinpath(DECISIONS_FILENAME), payload)
        with pytest.raises(IncompatibleArtifactError):
            read_decisions(tmp_path)

    def test_a_rejection_carrying_final_bounds_is_refused(self, tmp_path: Path) -> None:
        """A rejection has no approved interval, so it cannot have been given one."""
        candidate = selected()[0]
        payload = collection_of(
            RejectedDecision(
                candidate_id=candidate.id,
                original_start=candidate.start,
                original_end=candidate.end,
                reviewed_at=NOW,
            )
        ).model_dump(mode="json")
        payload["decisions"][0]["final_start"] = candidate.start
        payload["decisions"][0]["final_end"] = candidate.end
        write_json(tmp_path.joinpath(DECISIONS_FILENAME), payload)
        with pytest.raises(IncompatibleArtifactError):
            read_decisions(tmp_path)
