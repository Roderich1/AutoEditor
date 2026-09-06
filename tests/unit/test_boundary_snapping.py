"""CE-031: where a validated interval is moved to, and when it is left alone.

The default transcript is a ten-second grid: segment ``i`` covers
``[10i, 10i+9]`` and holds two words, ``[10i, 10i+0.5]`` and ``[10i+8.5, 10i+9]``.
That gives exactly two anchor sets, and every expected target below is one of
their members:

    start anchors   10i and 10i+8.5      (segment starts, word starts)
    end anchors     10i+0.5 and 10i+9    (word ends, segment ends)

Tests that need a tie build their own transcript, because the grid deliberately
has none.
"""

from __future__ import annotations

from content_engine.domain.candidate_rules import CandidatePolicy, snap_boundary
from content_engine.domain.enums import BoundaryAnchor
from content_engine.domain.models import Transcript, TranscriptSegment
from tests.conftest import CANDIDATE_POLICY, chunk_of, raw_candidate, speech_transcript

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


def _snap(
    start: float,
    end: float,
    policy: CandidatePolicy = CANDIDATE_POLICY,
    source_duration_seconds: float = SOURCE_DURATION,
    *,
    words: bool = True,
):
    return snap_boundary(
        raw_candidate(start, end),
        chunk_of(speech_transcript(words=words)),
        source_duration_seconds,
        policy,
    )


def _bare_transcript(bounds: list[tuple[float, float]], duration: float) -> Transcript:
    """A transcript of word-free segments, for building anchor sets by hand."""
    return Transcript(
        language="es",
        language_probability=0.99,
        duration_seconds=duration,
        declared_duration_seconds=duration,
        segments=[
            TranscriptSegment(index=index, start=start, end=end, text=f"s{index}", words=[])
            for index, (start, end) in enumerate(bounds)
        ],
        model="fake-model",
        created_at=speech_transcript().created_at,
    )


# --- movement ----------------------------------------------------------------


def test_both_ends_move_onto_the_nearest_segment_edges() -> None:
    adjustment = _snap(10.2, 39.4)

    assert (adjustment.adjusted_start, adjustment.adjusted_end) == (10.0, 39.0)
    assert adjustment.start_anchor is BoundaryAnchor.SEGMENT_START
    assert adjustment.end_anchor is BoundaryAnchor.SEGMENT_END
    assert not adjustment.reverted


def test_the_record_keeps_the_proposal_beside_the_adjustment() -> None:
    """ "Correct idea, bad boundary" is only measurable if both are kept."""
    adjustment = _snap(10.2, 39.4)

    assert (adjustment.proposed_start, adjustment.proposed_end) == (10.2, 39.4)
    assert round(adjustment.start_delta, 9) == -0.2
    assert round(adjustment.end_delta, 9) == -0.4
    assert adjustment.window_seconds == CANDIDATE_POLICY.boundary_snap_seconds


def test_an_endpoint_with_nothing_in_reach_is_left_where_it_was() -> None:
    """113.5 is 3.0 from the word end at 110.5 and 5.5 from the segment end at
    119.0, so a 2.5 s window reaches neither."""
    adjustment = _snap(90.0, 113.5)

    assert adjustment.adjusted_end == 113.5
    assert adjustment.end_anchor is BoundaryAnchor.UNCHANGED
    assert adjustment.end_delta == 0.0
    assert not adjustment.reverted


def test_an_endpoint_exactly_at_the_edge_of_the_window_still_snaps() -> None:
    """12.5 is exactly 2.5 s from the segment start at 10.0."""
    adjustment = _snap(12.5, 39.0)

    assert adjustment.adjusted_start == 10.0
    assert adjustment.start_anchor is BoundaryAnchor.SEGMENT_START


def test_an_endpoint_a_hair_beyond_the_window_does_not_move() -> None:
    """12.6 is 2.6 from 10.0, 4.1 from 8.5 and 5.9 from 18.5: nothing is in reach."""
    adjustment = _snap(12.6, 39.0)

    assert adjustment.adjusted_start == 12.6
    assert adjustment.start_anchor is BoundaryAnchor.UNCHANGED


def test_snapping_without_word_timestamps_uses_segment_edges_only() -> None:
    adjustment = _snap(10.2, 39.4, words=False)

    assert (adjustment.adjusted_start, adjustment.adjusted_end) == (10.0, 39.0)
    assert adjustment.start_anchor is BoundaryAnchor.SEGMENT_START


def test_an_interval_already_on_the_boundaries_records_no_movement() -> None:
    adjustment = _snap(10.0, 39.0)

    assert adjustment.start_delta == 0.0
    assert adjustment.end_delta == 0.0
    assert adjustment.start_anchor is BoundaryAnchor.SEGMENT_START
    assert not adjustment.reverted


# --- tie-breaks --------------------------------------------------------------


def test_a_segment_edge_beats_a_word_edge_at_the_same_distance() -> None:
    """Segment 3 is stretched to end at 39.5, leaving its last word ending at
    39.0. A proposal ending at 39.25 is 0.25 from both, and the segment wins."""
    transcript = speech_transcript()
    stretched = transcript.segments[3].model_copy(update={"end": 39.5})
    chunk = chunk_of(
        transcript.model_copy(
            update={
                "segments": [
                    *transcript.segments[:3],
                    stretched,
                    *transcript.segments[4:],
                ]
            }
        )
    )

    adjustment = snap_boundary(raw_candidate(10.0, 39.25), chunk, SOURCE_DURATION, CANDIDATE_POLICY)

    assert adjustment.adjusted_end == 39.5
    assert adjustment.end_anchor is BoundaryAnchor.SEGMENT_END


def test_a_start_tie_between_equal_anchors_takes_the_earlier_one() -> None:
    """Segment starts at 4.0 and 8.0 are both 2.0 from a proposal starting at
    6.0. The earlier wins, so a clip begins a moment early rather than late."""
    chunk = chunk_of(_bare_transcript([(4.0, 5.0), (8.0, 30.0)], 30.0))

    adjustment = snap_boundary(raw_candidate(6.0, 29.0), chunk, 30.0, CANDIDATE_POLICY)

    assert adjustment.adjusted_start == 4.0
    assert adjustment.start_anchor is BoundaryAnchor.SEGMENT_START


def test_an_end_tie_between_equal_anchors_takes_the_later_one() -> None:
    """The mirror rule: segment ends at 25.0 and 29.0 are both 2.0 from a
    proposal ending at 27.0, and the later wins. A clip that runs a moment long
    is watchable; one that stops a moment early loses the point."""
    chunk = chunk_of(_bare_transcript([(0.0, 25.0), (26.0, 29.0)], 29.0))

    adjustment = snap_boundary(raw_candidate(5.0, 27.0), chunk, 29.0, CANDIDATE_POLICY)

    assert adjustment.adjusted_end == 29.0
    assert adjustment.end_anchor is BoundaryAnchor.SEGMENT_END


# --- reversion ---------------------------------------------------------------


def test_an_adjustment_that_would_fall_under_the_minimum_is_reverted_whole() -> None:
    """A 20.1 s proposal whose ends snap inwards to 19.0 s would be refused by
    the duration policy the analyzer was asked to respect.

    Reverting rather than discarding is the rule: the analyzer judged this moment
    worth clipping, and an editorial refinement must not overrule that judgement.
    """
    adjustment = _snap(9.5, 29.6)

    assert adjustment.reverted
    assert (adjustment.adjusted_start, adjustment.adjusted_end) == (9.5, 29.6)
    assert adjustment.start_delta == 0.0
    assert adjustment.end_delta == 0.0
    assert adjustment.start_anchor is BoundaryAnchor.UNCHANGED
    assert adjustment.end_anchor is BoundaryAnchor.UNCHANGED
    assert adjustment.window_seconds == CANDIDATE_POLICY.boundary_snap_seconds


def test_an_adjustment_that_would_exceed_the_maximum_is_reverted() -> None:
    """A 29.4 s proposal whose end snaps outwards to 30.5 s would break a 30 s
    ceiling, so the whole adjustment is undone rather than half of it kept."""
    adjustment = _snap(0.6, 30.0, _policy(min_duration_seconds=1.0, max_duration_seconds=30.0))

    assert adjustment.reverted
    assert (adjustment.adjusted_start, adjustment.adjusted_end) == (0.6, 30.0)


def test_an_adjustment_that_would_pass_the_end_of_the_source_is_reverted() -> None:
    """117.5 snaps to the word end at 118.5, which is past 118 s of audio."""
    adjustment = _snap(
        10.0,
        117.5,
        _policy(min_duration_seconds=1.0, max_duration_seconds=200.0),
        source_duration_seconds=118.0,
    )

    assert adjustment.reverted


def test_reversion_undoes_both_ends_even_when_only_one_broke_the_policy() -> None:
    """Half an adjustment is a boundary nobody chose: the start would have moved
    for a reason that no longer applies once the end is put back."""
    adjustment = _snap(9.5, 29.6)

    assert adjustment.adjusted_start == 9.5
    assert adjustment.adjusted_end == 29.6


# --- purity ------------------------------------------------------------------


def test_snapping_is_a_pure_function_of_its_arguments() -> None:
    assert _snap(10.2, 39.4) == _snap(10.2, 39.4)
