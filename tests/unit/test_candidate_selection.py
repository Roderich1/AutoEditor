"""CE-032 and CE-033: deduplication, ranking, the ceiling, and the funnel.

These run the whole pipeline through ``select_candidates`` rather than calling
the pieces, because what is under test is mostly the interaction between them:
that the minimum-score filter runs before deduplication, that the top-N cut runs
last, that one priority order governs both, and that every proposal comes out in
exactly one terminal category with a record to prove it.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from content_engine.domain.candidate_rules import (
    CandidatePolicy,
    candidate_id,
    interval_iou,
    select_candidates,
)
from content_engine.domain.candidates import RawCandidate
from content_engine.domain.enums import CandidateStatus, RejectionReason
from content_engine.domain.scoring import calculate_total
from tests.conftest import (
    CANDIDATE_POLICY,
    PROMPT,
    TRANSCRIPT_SHA,
    chunk_of,
    collect,
    proposals_of,
    raw_candidate,
    scores,
    speech_transcript,
)

CHUNK = chunk_of(speech_transcript())
SOURCE_DURATION = 119.0


def _policy(**overrides: float) -> CandidatePolicy:
    values = {
        "min_duration_seconds": CANDIDATE_POLICY.min_duration_seconds,
        "max_duration_seconds": CANDIDATE_POLICY.max_duration_seconds,
        "min_score": CANDIDATE_POLICY.min_score,
        "target_candidates": CANDIDATE_POLICY.target_candidates,
        "max_candidates": CANDIDATE_POLICY.max_candidates,
        "dedupe_iou": CANDIDATE_POLICY.dedupe_iou,
        "boundary_snap_seconds": CANDIDATE_POLICY.boundary_snap_seconds,
    }
    values.update(overrides)
    return CandidatePolicy(**values)  # type: ignore[arg-type]


# --- CE-032: the overlap ratio itself ----------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param((10.0, 40.0), (10.0, 40.0), 1.0, id="identical"),
        pytest.param((10.0, 40.0), (20.0, 30.0), 10.0 / 30.0, id="contained"),
        pytest.param((10.0, 40.0), (30.0, 60.0), 10.0 / 50.0, id="partial"),
        pytest.param((10.0, 40.0), (50.0, 80.0), 0.0, id="disjoint"),
        pytest.param((10.0, 40.0), (40.0, 70.0), 0.0, id="touching"),
        pytest.param((40.0, 70.0), (10.0, 40.0), 0.0, id="touching-reversed"),
    ],
)
def test_the_overlap_ratio_matches_its_definition(
    first: tuple[float, float], second: tuple[float, float], expected: float
) -> None:
    assert interval_iou(*first, *second) == pytest.approx(expected)


def test_the_overlap_ratio_is_symmetric() -> None:
    assert interval_iou(10.0, 40.0, 30.0, 60.0) == interval_iou(30.0, 60.0, 10.0, 40.0)


def test_touching_intervals_are_not_duplicates() -> None:
    """Sharing an instant is not sharing a moment, and 0.0 is the honest answer
    rather than a division that has to be guarded."""
    assert interval_iou(10.0, 40.0, 40.0, 70.0) == 0.0


# --- CE-032: deduplication ---------------------------------------------------


def test_two_proposals_covering_the_same_moment_leave_one_survivor() -> None:
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0), raw_candidate(10.0, 39.0, hook=70)])

    assert collection.counts.selected == 1
    assert collection.counts.deduplicated == 1
    assert len(collection.deduplication_events) == 1


def test_the_stronger_candidate_is_the_one_kept() -> None:
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0, hook=40), raw_candidate(10.0, 39.0)])

    event = collection.deduplication_events[0]
    assert event.kept_score > event.dropped_score
    assert collection.candidates[0].id == event.kept_id


def test_a_dropped_duplicate_is_recorded_with_its_reason_and_no_rank() -> None:
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0), raw_candidate(10.0, 39.0, hook=70)])

    dropped = collection.rejected[0]
    assert dropped.status is CandidateStatus.DEDUPLICATED
    assert dropped.rejection_reasons == [RejectionReason.DUPLICATE]
    assert dropped.rank is None


#: [4, 64] against [24, 84]: 40 s of overlap over 80 s of union, exactly 0.5.
#: Every endpoint sits in a gap between anchor windows, so snapping leaves all
#: four alone and the ratio under test is the ratio of the intervals written here.
HALF_OVERLAP = (raw_candidate(4.0, 64.0), raw_candidate(24.0, 84.0, hook=70))


def test_an_overlap_exactly_at_the_threshold_is_a_duplicate() -> None:
    collection = collect(CHUNK, list(HALF_OVERLAP), _policy(dedupe_iou=0.5))

    assert collection.counts.deduplicated == 1
    assert collection.deduplication_events[0].iou == pytest.approx(0.5)


def test_an_overlap_just_under_the_threshold_is_not_a_duplicate() -> None:
    collection = collect(CHUNK, list(HALF_OVERLAP), _policy(dedupe_iou=0.51))

    assert collection.counts.deduplicated == 0
    assert collection.counts.selected == 2


def test_the_pair_at_the_threshold_really_is_left_alone_by_snapping() -> None:
    """Stated separately so the test above cannot quietly stop measuring what it
    says it measures if the snapping rules change."""
    collection = collect(CHUNK, list(HALF_OVERLAP), _policy(dedupe_iou=0.51))

    intervals = sorted((item.start, item.end) for item in collection.candidates)
    assert intervals == [(4.0, 64.0), (24.0, 84.0)]


def test_a_chain_that_is_not_transitive_keeps_the_far_end() -> None:
    """A absorbs B; C overlaps B but not A, so C really is a different moment.

    A greedy pass over the priority order gets this right without the relation
    having to be transitive, which it is not.
    """
    collection = collect(
        CHUNK,
        [
            raw_candidate(0.0, 40.0),  # A, strongest
            raw_candidate(5.0, 45.0, hook=80),  # B, a duplicate of A
            raw_candidate(30.0, 70.0, hook=70),  # C, overlaps B but not A
        ],
        _policy(dedupe_iou=0.6),
    )

    assert collection.counts.deduplicated == 1
    assert collection.counts.selected == 2
    assert collection.deduplication_events[0].kept_id == collection.candidates[0].id


def test_a_candidate_matching_several_survivors_is_dropped_by_the_strongest() -> None:
    collection = collect(
        CHUNK,
        [
            raw_candidate(10.0, 40.0),  # strongest
            raw_candidate(10.0, 41.0, hook=90),
            raw_candidate(10.0, 40.5, hook=50),
        ],
        _policy(dedupe_iou=0.6, min_score=0.0),
    )

    keepers = {event.kept_id for event in collection.deduplication_events}
    assert keepers == {collection.candidates[0].id}


def test_deduplication_runs_across_chunks_not_within_them() -> None:
    """Overlapping windows return the same moment twice; that is the whole
    reason this step exists."""
    first = chunk_of(speech_transcript(), chunk_id="chunk_0000")
    second = chunk_of(speech_transcript(), chunk_id="chunk_0001")
    proposals = [
        *proposals_of(first, [raw_candidate(10.0, 39.0)]),
        *proposals_of(second, [raw_candidate(10.0, 39.0)]),
    ]

    collection = select_candidates(
        proposals, SOURCE_DURATION, CANDIDATE_POLICY, 1, datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert collection.counts.proposed == 2
    assert collection.counts.deduplicated == 1
    assert collection.counts.selected == 1


def test_a_candidate_below_the_minimum_never_absorbs_a_good_one() -> None:
    """The score filter runs first, so a weak candidate is gone before it could
    be the reason a strong one disappears."""
    collection = collect(
        CHUNK,
        [raw_candidate(10.0, 39.0, hook=0), raw_candidate(10.0, 39.0, hook=92)],
        _policy(min_score=80.0),
    )

    assert collection.counts.below_min_score == 1
    assert collection.counts.deduplicated == 0
    assert collection.counts.selected == 1


# --- CE-033: the minimum score ----------------------------------------------


def test_a_total_exactly_at_the_minimum_survives() -> None:
    """min_score is the lowest score worth a human's attention, not the lowest
    disqualifying one."""
    exact = calculate_total(scores(92))
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0)], _policy(min_score=exact))

    assert collection.counts.selected == 1
    assert collection.counts.below_min_score == 0


def test_a_total_just_under_the_minimum_is_refused() -> None:
    exact = calculate_total(scores(92))
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0)], _policy(min_score=exact + 0.01))

    assert collection.counts.below_min_score == 1
    assert collection.rejected[0].rejection_reasons == [RejectionReason.BELOW_MIN_SCORE]


def test_a_total_just_over_the_minimum_survives() -> None:
    exact = calculate_total(scores(92))
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0)], _policy(min_score=exact - 0.01))

    assert collection.counts.selected == 1


# --- CE-033: ranking and the ceiling -----------------------------------------


def _spread(count: int) -> list:
    """`count` separate 20 s candidates with strictly decreasing scores.

    A 22 s stride keeps five of them inside 119 seconds of audio, so a test
    about the ceiling is about the ceiling and not about proposals that reach
    past the end of the source.
    """
    return [
        raw_candidate(index * 22.0, index * 22.0 + 20.0, hook=90 - index) for index in range(count)
    ]


def test_the_spread_used_by_the_ceiling_tests_is_entirely_valid() -> None:
    collection = collect(CHUNK, _spread(5), _policy(max_candidates=15))

    assert collection.counts.selected == 5
    assert collection.counts.invalid == 0
    assert collection.counts.deduplicated == 0


def test_ranks_are_contiguous_from_one_in_descending_score_order() -> None:
    collection = collect(CHUNK, _spread(4))

    assert [candidate.rank for candidate in collection.candidates] == [1, 2, 3, 4]
    totals = [candidate.total_score for candidate in collection.candidates]
    assert totals == sorted(totals, reverse=True)


def test_equal_scores_are_ordered_by_the_earlier_interval() -> None:
    """Two disjoint moments with identical ratings: only the clock separates them."""
    collection = collect(CHUNK, [raw_candidate(60.0, 85.0), raw_candidate(10.0, 35.0)])

    assert [candidate.start for candidate in collection.candidates] == [10.0, 60.0]


def test_a_full_tie_is_settled_by_the_identifier() -> None:
    """Same score and the same start, different ends, overlapping too little to
    be duplicates. Nothing but the identifier can separate them, and it can."""
    collection = collect(CHUNK, [raw_candidate(10.0, 100.0), raw_candidate(10.0, 35.0)])

    assert collection.counts.selected == 2
    assert [candidate.start for candidate in collection.candidates] == [10.0, 10.0]
    totals = {candidate.total_score for candidate in collection.candidates}
    assert len(totals) == 1
    identifiers = [candidate.id for candidate in collection.candidates]
    assert identifiers == sorted(identifiers)


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5])
def test_the_ceiling_cuts_only_what_exceeds_it(count: int) -> None:
    collection = collect(CHUNK, _spread(count), _policy(max_candidates=3))

    assert collection.counts.selected == min(count, 3)
    assert collection.counts.not_in_top_n == max(count - 3, 0)


def test_a_candidate_cut_by_the_ceiling_is_recorded_not_discarded() -> None:
    collection = collect(CHUNK, _spread(4), _policy(max_candidates=2))

    cut = [
        candidate
        for candidate in collection.rejected
        if candidate.rejection_reasons == [RejectionReason.NOT_IN_TOP_N]
    ]
    assert len(cut) == 2
    assert all(candidate.rank is None for candidate in cut)
    assert all(candidate.status is CandidateStatus.REJECTED for candidate in cut)


def test_the_ceiling_takes_the_highest_scoring_candidates() -> None:
    collection = collect(CHUNK, _spread(4), _policy(max_candidates=2))

    selected = {candidate.total_score for candidate in collection.candidates}
    cut = {
        candidate.total_score
        for candidate in collection.rejected
        if candidate.rejection_reasons == [RejectionReason.NOT_IN_TOP_N]
    }
    assert min(selected) > max(cut)


def test_the_run_target_is_recorded_but_never_cuts_anything() -> None:
    """ADR-021: target_candidates is an objective for the run, and max_candidates
    is the only ceiling. A target of one must not reduce a shortlist of four."""
    collection = collect(CHUNK, _spread(4), _policy(target_candidates=1, max_candidates=15))

    assert collection.target_candidates == 1
    assert collection.counts.selected == 4
    assert collection.counts.not_in_top_n == 0


# --- identity and determinism ------------------------------------------------


def test_two_identical_proposals_in_one_batch_keep_distinct_identifiers() -> None:
    """Otherwise the collection would refuse the pair, and the funnel would lose
    the very record that says a duplicate was proposed."""
    collection = collect(CHUNK, [raw_candidate(10.0, 39.0), raw_candidate(10.0, 39.0)])

    identifiers = [candidate.id for candidate in (*collection.candidates, *collection.rejected)]
    assert len(set(identifiers)) == 2


def test_the_same_proposal_from_two_chunks_keeps_distinct_identifiers() -> None:
    first = candidate_id(TRANSCRIPT_SHA, "chunk_0000", 0, raw_candidate(10.0, 39.0), PROMPT)
    second = candidate_id(TRANSCRIPT_SHA, "chunk_0001", 0, raw_candidate(10.0, 39.0), PROMPT)

    assert first != second


def test_an_identifier_survives_the_json_round_trip_of_its_proposal() -> None:
    """The identifier is recomputed on a rerun from a proposal that has been
    written to candidates.raw.json and read back. If it changed there, every
    identifier in the artifacts would be unreproducible.
    """
    proposal = raw_candidate(10.0, 39.0)
    restored = RawCandidate.model_validate(proposal.model_dump(mode="json"))

    assert candidate_id(TRANSCRIPT_SHA, "chunk_0000", 0, proposal, PROMPT) == candidate_id(
        TRANSCRIPT_SHA, "chunk_0000", 0, restored, PROMPT
    )


def test_an_identifier_changes_with_the_transcript_it_came_from() -> None:
    first = candidate_id(TRANSCRIPT_SHA, "chunk_0000", 0, raw_candidate(10.0, 39.0), PROMPT)
    second = candidate_id("0" * 64, "chunk_0000", 0, raw_candidate(10.0, 39.0), PROMPT)

    assert first != second


def test_an_identifier_is_derived_from_the_proposal_not_the_adjusted_interval() -> None:
    """It has to be, because it is the last tie-break in the ordering that runs
    before snapping has happened."""
    expected = candidate_id(TRANSCRIPT_SHA, CHUNK.id, 0, raw_candidate(10.2, 39.4), PROMPT)

    collection = collect(CHUNK, [raw_candidate(10.2, 39.4)])

    assert collection.candidates[0].id == expected
    assert collection.candidates[0].start == 10.0


def test_the_result_does_not_depend_on_the_order_the_proposals_arrive_in() -> None:
    candidates = [
        raw_candidate(10.0, 39.0),
        raw_candidate(10.0, 39.0, hook=70),
        raw_candidate(-5.0, 30.0),
        raw_candidate(50.0, 55.0),
        raw_candidate(60.0, 85.0, hook=10),
        raw_candidate(90.0, 115.0, hook=80),
    ]
    proposals = proposals_of(CHUNK, candidates)
    reference = select_candidates(
        proposals, SOURCE_DURATION, CANDIDATE_POLICY, 1, datetime(2026, 1, 1, tzinfo=UTC)
    ).model_dump(mode="json")

    for permutation in itertools.islice(itertools.permutations(proposals), 24):
        shuffled = select_candidates(
            list(permutation),
            SOURCE_DURATION,
            CANDIDATE_POLICY,
            1,
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert shuffled.model_dump(mode="json") == reference


# --- the funnel --------------------------------------------------------------


def test_every_proposal_lands_in_exactly_one_terminal_category() -> None:
    collection = collect(
        CHUNK,
        [
            raw_candidate(10.0, 39.0),  # selected
            raw_candidate(10.0, 39.0, hook=70),  # deduplicated
            raw_candidate(-5.0, 30.0),  # invalid
            raw_candidate(60.0, 85.0, hook=0),  # 68.15, below the 80 asked for here
            raw_candidate(90.0, 115.0, hook=80),  # selected
        ],
        _policy(min_score=80.0, max_candidates=2),
    )
    counts = collection.counts

    assert counts.proposed == 5
    assert counts.invalid == 1
    assert counts.below_min_score == 1
    assert counts.deduplicated == 1
    assert counts.selected == 2
    assert counts.not_in_top_n == 0


def test_no_proposal_is_dropped_without_a_record() -> None:
    candidates = [
        raw_candidate(10.0, 39.0),
        raw_candidate(10.0, 39.0, hook=70),
        raw_candidate(-5.0, 30.0),
        raw_candidate(60.0, 85.0, hook=0),
    ]
    collection = collect(CHUNK, candidates, _policy(min_score=80.0))

    recorded = len(collection.candidates) + len(collection.rejected) + len(collection.invalid)
    assert recorded == len(candidates)


def test_an_empty_set_of_proposals_produces_an_empty_collection() -> None:
    collection = collect(CHUNK, [])

    assert collection.counts.proposed == 0
    assert collection.candidates == []
    assert collection.rejected == []
    assert collection.invalid == []
    assert collection.deduplication_events == []


def test_every_deduplication_event_is_backed_by_the_records_it_names() -> None:
    """The collection validates this itself; asserting it here says the pipeline
    produces events that survive that validation rather than merely that the
    validation exists."""
    collection = collect(
        CHUNK,
        [raw_candidate(10.0, 39.0), raw_candidate(10.0, 39.0, hook=70)],
    )
    by_id = {
        candidate.id: candidate for candidate in (*collection.candidates, *collection.rejected)
    }

    for event in collection.deduplication_events:
        assert by_id[event.kept_id].total_score == event.kept_score
        assert by_id[event.dropped_id].status is CandidateStatus.DEDUPLICATED
        assert event.iou >= collection.dedupe_iou
