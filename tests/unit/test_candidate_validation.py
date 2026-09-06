"""CE-030: which proposals are refused before anything is computed about them.

The transcript under test is a ten-second grid — segments [0, 9], [10, 19] and
so on — so every expected answer is readable from the numbers in the test rather
than derived from the implementation. The one second of silence between
consecutive segments is where an ungrounded timestamp can live, and the words
sit at the first and last half-second of each segment.
"""

from __future__ import annotations

import pytest

from content_engine.domain.candidate_rules import (
    REASON_ORDER,
    CandidatePolicy,
    is_grounded,
    validate_proposal,
)
from content_engine.domain.enums import PRE_SCORING_REASONS, RejectionReason
from tests.conftest import (
    CANDIDATE_POLICY,
    chunk_of,
    raw_candidate,
    speech_transcript,
)

SOURCE_DURATION = 119.0


def _chunk(**kwargs: bool):
    return chunk_of(speech_transcript(**kwargs))


def _reasons(
    start: float,
    end: float,
    policy: CandidatePolicy = CANDIDATE_POLICY,
    source_duration_seconds: float = SOURCE_DURATION,
    *,
    words: bool = True,
) -> list[RejectionReason]:
    return validate_proposal(
        raw_candidate(start, end), _chunk(words=words), source_duration_seconds, policy
    )


# --- a proposal that breaks nothing -----------------------------------------


def test_a_well_formed_grounded_proposal_is_accepted() -> None:
    assert _reasons(10.0, 39.0) == []


def test_a_proposal_at_exactly_the_minimum_duration_is_accepted() -> None:
    """The policy is a range, so its endpoints are inside it."""
    assert _reasons(10.0, 30.0) == []


def test_a_proposal_at_exactly_the_maximum_duration_is_accepted() -> None:
    assert _reasons(10.0, 100.0) == []


def test_a_proposal_ending_exactly_at_the_source_duration_is_accepted() -> None:
    assert _reasons(50.0, 119.0) == []


# --- interval rules ----------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(-5.0, 30.0, id="negative-start"),
        pytest.param(40.0, 40.0, id="zero-length"),
        pytest.param(60.0, 30.0, id="inverted"),
    ],
)
def test_an_impossible_interval_is_refused(start: float, end: float) -> None:
    assert RejectionReason.INVALID_INTERVAL in _reasons(start, end)


def test_a_zero_length_interval_is_not_also_called_too_short() -> None:
    """There is no length to compare, so claiming one would invent a measurement."""
    reasons = _reasons(40.0, 40.0)

    assert RejectionReason.INVALID_INTERVAL in reasons
    assert RejectionReason.TOO_SHORT not in reasons
    assert RejectionReason.TOO_LONG not in reasons


def test_an_inverted_interval_is_not_given_a_negative_duration_verdict() -> None:
    reasons = _reasons(60.0, 30.0)

    assert RejectionReason.TOO_SHORT not in reasons


# --- containment rules -------------------------------------------------------


def test_a_proposal_starting_before_the_chunk_is_refused() -> None:
    transcript = speech_transcript()
    chunk = chunk_of(transcript)
    later = chunk.model_copy(update={"start": 20.0})

    reasons = validate_proposal(raw_candidate(10.0, 40.0), later, SOURCE_DURATION, CANDIDATE_POLICY)

    assert RejectionReason.OUTSIDE_CHUNK in reasons


def test_a_proposal_ending_after_the_chunk_is_refused() -> None:
    chunk = chunk_of(speech_transcript(count=4))

    reasons = validate_proposal(raw_candidate(10.0, 60.0), chunk, SOURCE_DURATION, CANDIDATE_POLICY)

    assert RejectionReason.OUTSIDE_CHUNK in reasons


def test_a_proposal_reaching_the_exact_chunk_bounds_is_contained() -> None:
    chunk = _chunk()

    reasons = validate_proposal(
        raw_candidate(chunk.start, chunk.end), chunk, SOURCE_DURATION, CANDIDATE_POLICY
    )

    assert RejectionReason.OUTSIDE_CHUNK not in reasons


def test_an_end_beyond_the_source_is_refused() -> None:
    """Separate from OUTSIDE_CHUNK: one is a windowing error, the other is a
    claim about audio that does not exist."""
    chunk = _chunk()
    stretched = chunk.model_copy(update={"end": 500.0})

    reasons = validate_proposal(
        raw_candidate(10.0, 200.0), stretched, SOURCE_DURATION, CANDIDATE_POLICY
    )

    assert RejectionReason.END_BEYOND_SOURCE in reasons


# --- duration policy ---------------------------------------------------------


def test_a_proposal_shorter_than_the_minimum_is_refused() -> None:
    assert RejectionReason.TOO_SHORT in _reasons(10.0, 25.0)


def test_a_proposal_longer_than_the_maximum_is_refused() -> None:
    assert RejectionReason.TOO_LONG in _reasons(0.0, 100.5)


def test_a_duration_a_hair_under_the_minimum_is_still_accepted() -> None:
    """The tolerance is explicit, so a float artefact cannot decide this."""
    assert RejectionReason.TOO_SHORT not in _reasons(10.0, 30.0 - 1e-9)


def test_a_duration_a_hair_over_the_maximum_is_still_accepted() -> None:
    assert RejectionReason.TOO_LONG not in _reasons(10.0, 100.0 + 1e-9)


# --- grounding ---------------------------------------------------------------


def test_an_instant_inside_a_segment_is_grounded() -> None:
    """A timestamp in the middle of a long sentence is supported by it, even
    though it is far from both of the sentence's edges."""
    chunk = _chunk()

    assert is_grounded(4.5, chunk, snap_seconds=0.001)


def test_an_instant_on_a_segment_edge_is_grounded() -> None:
    chunk = _chunk()

    assert is_grounded(9.0, chunk, snap_seconds=0.0)


def test_an_instant_within_the_snap_window_of_an_edge_is_grounded() -> None:
    """The reach is exactly what CE-031 would have had, which is the whole
    argument for choosing this tolerance rather than another number."""
    chunk = _chunk()

    assert is_grounded(9.5, chunk, snap_seconds=2.5)


def test_an_instant_beyond_the_snap_window_is_not_grounded() -> None:
    """11.6 is 2.6 s past the segment that ends at 9.0 and 2.6 s before the one
    that starts at 14.2, so neither edge is in reach of a 2.5 s window."""
    transcript = speech_transcript(count=2)
    second = transcript.segments[1]
    shifted = second.model_copy(
        update={
            "start": 14.2,
            "end": 19.0,
            "words": [
                word.model_copy(update={"start": 14.2, "end": 14.7}) for word in second.words[:1]
            ],
        }
    )
    chunk = chunk_of(transcript.model_copy(update={"segments": [transcript.segments[0], shifted]}))

    assert not is_grounded(11.6, chunk, snap_seconds=2.5)
    assert is_grounded(11.5, chunk, snap_seconds=2.5)


def test_a_long_gap_with_no_transcript_leaves_a_proposal_ungrounded() -> None:
    """Two segments with a minute of silence between them: a candidate proposed
    inside that silence is describing audio the transcript says nothing about."""
    transcript = speech_transcript(count=2)
    far = transcript.segments[1].model_copy(update={"index": 1, "start": 90.0, "end": 99.0})
    spread = transcript.model_copy(
        update={
            "segments": [
                transcript.segments[0],
                far.model_copy(update={"words": []}),
            ],
            "duration_seconds": 99.0,
        }
    )
    chunk = chunk_of(spread)

    assert not is_grounded(45.0, chunk, snap_seconds=2.5)
    reasons = validate_proposal(raw_candidate(45.0, 70.0), chunk, 99.0, CANDIDATE_POLICY)
    assert RejectionReason.UNGROUNDED in reasons


def test_a_chunk_without_word_timestamps_can_still_ground_on_segments() -> None:
    """word_timestamps = false degrades precision; it must not refuse everything."""
    assert _reasons(10.0, 39.0, words=False) == []


def test_word_edges_never_extend_the_reach_beyond_the_segment_that_holds_them() -> None:
    """Stated because the rule names both, and only one of them can ever decide.

    TranscriptSegment enforces that a word lies inside its segment, so for any
    instant outside every segment the nearest segment edge is at least as close
    as the nearest word edge. Word edges are still consulted — the contract names
    them, and a transcript model that stopped enforcing containment would need
    them — but a candidate can never be grounded by a word alone.
    """
    chunk = chunk_of(speech_transcript())
    segment_only = [edge for segment in chunk.segments for edge in (segment.start, segment.end)]
    word_only = [
        edge
        for segment in chunk.segments
        for word in segment.words
        for edge in (word.start, word.end)
    ]

    for instant in (9.4, 9.6, 11.5, 11.6, 25.0, 119.5):
        inside = any(segment.start <= instant <= segment.end for segment in chunk.segments)
        nearest_segment = min(abs(instant - edge) for edge in segment_only)
        nearest_word = min(abs(instant - edge) for edge in word_only)
        assert inside or nearest_segment <= nearest_word


# --- accumulation ------------------------------------------------------------


def test_every_applicable_reason_is_reported_not_only_the_first() -> None:
    chunk = _chunk()
    stretched = chunk.model_copy(update={"end": 500.0})

    reasons = validate_proposal(
        raw_candidate(-5.0, 300.0), stretched, SOURCE_DURATION, CANDIDATE_POLICY
    )

    assert reasons == [
        RejectionReason.INVALID_INTERVAL,
        RejectionReason.OUTSIDE_CHUNK,
        RejectionReason.END_BEYOND_SOURCE,
        RejectionReason.TOO_LONG,
        RejectionReason.UNGROUNDED,
    ]


def test_a_negative_and_too_short_proposal_reports_both() -> None:
    reasons = _reasons(-2.0, 5.0)

    assert RejectionReason.INVALID_INTERVAL in reasons
    assert RejectionReason.TOO_SHORT in reasons


def test_outside_the_chunk_and_beyond_the_source_are_reported_together() -> None:
    chunk = chunk_of(speech_transcript(count=4))

    reasons = validate_proposal(
        raw_candidate(10.0, 150.0), chunk, SOURCE_DURATION, CANDIDATE_POLICY
    )

    assert RejectionReason.OUTSIDE_CHUNK in reasons
    assert RejectionReason.END_BEYOND_SOURCE in reasons


def test_the_reasons_are_reported_in_one_fixed_order() -> None:
    """A stable order is what makes two runs diffable at all."""
    chunk = _chunk().model_copy(update={"end": 500.0})
    reasons = validate_proposal(
        raw_candidate(-5.0, 300.0), chunk, SOURCE_DURATION, CANDIDATE_POLICY
    )

    positions = [REASON_ORDER.index(reason) for reason in reasons]
    assert positions == sorted(positions)


def test_every_reason_ce_030_can_produce_belongs_to_the_pre_scoring_phase() -> None:
    """CE-030 runs before a score exists, so it can cite nothing that needs one."""
    assert set(REASON_ORDER) == set(PRE_SCORING_REASONS)
