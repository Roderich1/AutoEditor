"""Which clips a completed review asks for, and over which seconds.

This is the step where somebody's judgement becomes a list of intervals, so it
is the step where a mistake renders material a person rejected -- or renders the
analyzer's boundaries over an edit they took the trouble to move. Nothing here
consults the candidate's own interval once a decision exists: an approval keeps
it, an edit replaces it, and a rejection removes the candidate entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from content_engine.domain.candidates import ValidatedCandidate
from content_engine.domain.enums import ReviewDecisionType
from content_engine.domain.exceptions import IncompatibleArtifactError
from content_engine.domain.render_targets import build_render_target
from content_engine.domain.review import (
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
    ReviewDecisionCollection,
)
from tests.conftest import CANDIDATE_POLICY, chunk_of, collect, raw_candidate, speech_transcript

DIGEST = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def shortlist(count: int = 3) -> list[ValidatedCandidate]:
    """``count`` selected candidates on the shared ten-second transcript grid."""
    transcript = speech_transcript()
    chunk = chunk_of(transcript)
    proposals = [raw_candidate(index * 30.0, index * 30.0 + 29.0) for index in range(count)]
    collection = collect(chunk, proposals, CANDIDATE_POLICY, transcript.duration_seconds)
    assert len(collection.candidates) == count, collection.counts
    return collection.candidates


def collection_of(*decisions: object, duration: float = 119.0) -> ReviewDecisionCollection:
    return ReviewDecisionCollection(
        analysis_fingerprint=DIGEST,
        source_duration_seconds=duration,
        created_at=NOW,
        updated_at=NOW,
        decisions=list(decisions),
    )


def approve(candidate: ValidatedCandidate) -> ApprovedDecision:
    return ApprovedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=NOW,
        final_start=candidate.start,
        final_end=candidate.end,
    )


def reject(candidate: ValidatedCandidate) -> RejectedDecision:
    return RejectedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=NOW,
    )


def edit(candidate: ValidatedCandidate, start: float, end: float) -> EditedDecision:
    return EditedDecision(
        candidate_id=candidate.id,
        original_start=candidate.start,
        original_end=candidate.end,
        reviewed_at=NOW,
        final_start=start,
        final_end=end,
    )


def target(candidates: list[ValidatedCandidate], collection: ReviewDecisionCollection) -> object:
    return build_render_target(
        candidates=tuple(candidates),
        decisions=collection,
        analysis_fingerprint=DIGEST,
        review_fingerprint="b" * 64,
        decisions_sha256="c" * 64,
        transcript_sha256="d" * 64,
        source_sha256="e" * 64,
        source_duration_seconds=119.0,
    )


class TestSelection:
    def test_an_approved_candidate_keeps_its_original_bounds(self) -> None:
        candidates = shortlist(1)
        built = target(candidates, collection_of(approve(candidates[0])))

        assert len(built.clips) == 1
        clip = built.clips[0]
        assert clip.start == candidates[0].start
        assert clip.end == candidates[0].end
        assert clip.decision is ReviewDecisionType.APPROVED

    def test_an_edited_candidate_uses_only_the_final_bounds(self) -> None:
        candidates = shortlist(1)
        built = target(candidates, collection_of(edit(candidates[0], 5.0, 45.0)))

        clip = built.clips[0]
        assert (clip.start, clip.end) == (5.0, 45.0)
        assert clip.decision is ReviewDecisionType.EDITED
        assert clip.duration == pytest.approx(40.0)

    def test_a_rejected_candidate_produces_no_clip(self) -> None:
        candidates = shortlist(2)
        built = target(candidates, collection_of(reject(candidates[0]), approve(candidates[1])))

        assert [clip.candidate.id for clip in built.clips] == [candidates[1].id]

    def test_a_review_that_rejected_everything_produces_no_clips(self) -> None:
        candidates = shortlist(2)
        built = target(candidates, collection_of(reject(candidates[0]), reject(candidates[1])))

        assert built.clips == ()

    def test_the_original_bounds_are_carried_beside_the_final_ones(self) -> None:
        candidates = shortlist(1)
        built = target(candidates, collection_of(edit(candidates[0], 5.0, 45.0)))

        clip = built.clips[0]
        assert clip.original_start == candidates[0].start
        assert clip.original_end == candidates[0].end


class TestOrdering:
    def test_clips_come_out_in_rank_order(self) -> None:
        candidates = shortlist(3)
        built = target(
            candidates,
            collection_of(*(approve(candidate) for candidate in reversed(candidates))),
        )

        ranks = [clip.candidate.rank for clip in built.clips]
        assert ranks == sorted(ranks)

    def test_the_order_of_the_decisions_changes_nothing(self) -> None:
        candidates = shortlist(3)
        forwards = target(
            candidates, collection_of(*(approve(candidate) for candidate in candidates))
        )
        backwards = target(
            candidates,
            collection_of(*(approve(candidate) for candidate in reversed(candidates))),
        )

        assert [clip.candidate.id for clip in forwards.clips] == [
            clip.candidate.id for clip in backwards.clips
        ]

    def test_ranks_keep_their_gaps(self) -> None:
        candidates = shortlist(3)
        built = target(
            candidates, collection_of(reject(candidates[1]), *map(approve, candidates[::2]))
        )

        assert [clip.candidate.rank for clip in built.clips] == [1, 3]


class TestRefusals:
    def test_an_undecided_candidate_stops_the_render(self) -> None:
        candidates = shortlist(2)

        with pytest.raises(IncompatibleArtifactError, match="no decision"):
            target(candidates, collection_of(approve(candidates[0])))

    def test_the_message_names_how_many_are_missing(self) -> None:
        candidates = shortlist(3)

        with pytest.raises(IncompatibleArtifactError, match="2 of 3"):
            target(candidates, collection_of(approve(candidates[0])))

    def test_a_decision_for_an_unknown_candidate_stops_the_render(self) -> None:
        candidates = shortlist(1)
        stranger = collection_of(
            approve(candidates[0]),
            RejectedDecision(
                candidate_id="cand_unknown",
                original_start=1.0,
                original_end=2.0,
                reviewed_at=NOW,
            ),
        )

        with pytest.raises(IncompatibleArtifactError, match="not one of"):
            target(candidates, stranger)

    def test_an_edit_reaching_past_the_source_cannot_be_recorded_at_all(self) -> None:
        """Refused two layers earlier, which is why the renderer needs no check.

        ``ReviewDecisionCollection`` refuses a final interval past the duration
        it declares, and ``decisions_coherence_problem`` refuses a collection
        whose declared duration is not the run's. Between them an edit reaching
        past the source cannot reach the renderer, so a third check here would
        be unreachable code pretending to be a safeguard.
        """
        candidates = shortlist(1)

        with pytest.raises(ValueError, match="beyond the source"):
            collection_of(edit(candidates[0], 5.0, 200.0))

    def test_decisions_taken_against_another_duration_stop_the_render(self) -> None:
        candidates = shortlist(1)
        collection = collection_of(edit(candidates[0], 5.0, 200.0), duration=300.0)

        with pytest.raises(IncompatibleArtifactError, match="source duration"):
            target(candidates, collection)

    def test_a_decision_recording_other_original_bounds_stops_the_render(self) -> None:
        candidates = shortlist(1)
        drifted = collection_of(
            ApprovedDecision(
                candidate_id=candidates[0].id,
                original_start=candidates[0].start + 3.0,
                original_end=candidates[0].end + 3.0,
                reviewed_at=NOW,
                final_start=candidates[0].start + 3.0,
                final_end=candidates[0].end + 3.0,
            )
        )

        with pytest.raises(IncompatibleArtifactError, match="original_start"):
            target(candidates, drifted)

    def test_decisions_from_another_analysis_stop_the_render(self) -> None:
        candidates = shortlist(1)
        other = ReviewDecisionCollection(
            analysis_fingerprint="9" * 64,
            source_duration_seconds=119.0,
            created_at=NOW,
            updated_at=NOW,
            decisions=[approve(candidates[0])],
        )

        with pytest.raises(IncompatibleArtifactError, match="analysis"):
            target(candidates, other)

    def test_an_unranked_candidate_stops_the_render(self) -> None:
        candidate = shortlist(1)[0].model_copy(update={"rank": None})

        with pytest.raises(IncompatibleArtifactError, match="rank"):
            target([candidate], collection_of(approve(candidate)))
