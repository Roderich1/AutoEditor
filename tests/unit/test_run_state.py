"""The run state machine, checked against a table written from ADR-018.

Nothing here derives an expectation from the production tables. EXPECTED below
is transcribed by hand from the ADR; if the implementation and the ADR ever
disagree, this file fails. A test that rebuilt the matrix from SUCCESS_PATH and
STAGE_OUTCOMES would pass for any implementation those two produce, including a
wrong one, which is worth nothing.
"""

from __future__ import annotations

import pytest

from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import InvalidRunStateError
from content_engine.domain.run_state import (
    ALLOWED_TRANSITIONS,
    RE_ENTRY_TRANSITIONS,
    SUCCESS_PATH,
    failure_status,
    is_allowed,
    stage_for_failure,
    success_status,
    validate_transition,
)

S = RunStatus

#: ADR-018, written out in full. Self-transitions are excluded: writing the same
#: state twice is a no-op and is asserted separately.
#:
#: A run moves forward one stage at a time; a stage may fail from the state that
#: precedes it or from its own success state when re-run; from a failure state a
#: retry reaches that stage's success state or fails again.
#:
#: ADR-030 adds the single backwards edge in the machine, REVIEWED ->
#: READY_FOR_REVIEW, which `review --force` takes when it discards a set of
#: human decisions. It is written into this table by hand like everything else,
#: so adding a second backwards edge means editing the ADR and this file.
EXPECTED: dict[RunStatus, set[RunStatus]] = {
    S.CREATED: {S.INSPECTED, S.FAILED_INSPECT},
    S.INSPECTED: {S.AUDIO_READY, S.FAILED_AUDIO, S.FAILED_INSPECT},
    S.AUDIO_READY: {S.TRANSCRIBED, S.FAILED_TRANSCRIPTION, S.FAILED_AUDIO},
    S.TRANSCRIBED: {S.ANALYZED, S.FAILED_ANALYSIS, S.FAILED_TRANSCRIPTION},
    S.ANALYZED: {S.READY_FOR_REVIEW, S.FAILED_ANALYSIS, S.FAILED_PREVIEW},
    S.READY_FOR_REVIEW: {S.REVIEWED, S.FAILED_PREVIEW, S.FAILED_REVIEW},
    S.REVIEWED: {S.RENDERED, S.FAILED_RENDER, S.FAILED_REVIEW, S.READY_FOR_REVIEW},
    S.RENDERED: {S.COMPLETED, S.FAILED_RENDER},
    S.COMPLETED: set(),
    S.FAILED_INSPECT: {S.INSPECTED, S.FAILED_INSPECT},
    S.FAILED_AUDIO: {S.AUDIO_READY, S.FAILED_AUDIO},
    S.FAILED_TRANSCRIPTION: {S.TRANSCRIBED, S.FAILED_TRANSCRIPTION},
    S.FAILED_ANALYSIS: {S.ANALYZED, S.FAILED_ANALYSIS},
    S.FAILED_PREVIEW: {S.READY_FOR_REVIEW, S.FAILED_PREVIEW},
    S.FAILED_REVIEW: {S.REVIEWED, S.FAILED_REVIEW},
    S.FAILED_RENDER: {S.RENDERED, S.FAILED_RENDER},
}

#: Which stage owns which pair of outcomes, also from the ADR.
EXPECTED_OUTCOMES: dict[RunStage, tuple[RunStatus, RunStatus]] = {
    RunStage.INSPECT: (S.INSPECTED, S.FAILED_INSPECT),
    RunStage.AUDIO: (S.AUDIO_READY, S.FAILED_AUDIO),
    RunStage.TRANSCRIPTION: (S.TRANSCRIBED, S.FAILED_TRANSCRIPTION),
    RunStage.ANALYSIS: (S.ANALYZED, S.FAILED_ANALYSIS),
    RunStage.PREVIEW: (S.READY_FOR_REVIEW, S.FAILED_PREVIEW),
    RunStage.REVIEW: (S.REVIEWED, S.FAILED_REVIEW),
    RunStage.RENDER: (S.RENDERED, S.FAILED_RENDER),
}

ALL_PAIRS = [(current, target) for current in RunStatus for target in RunStatus]


def test_every_status_appears_in_the_expected_table() -> None:
    """The hand-written table is complete, so the sweep below covers everything."""
    assert set(EXPECTED) == set(RunStatus)


def test_the_implementation_matches_the_table_from_the_adr() -> None:
    assert {status: set(targets) for status, targets in ALLOWED_TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize(("current", "target"), ALL_PAIRS)
def test_no_transition_outside_the_table_is_accepted(current: RunStatus, target: RunStatus) -> None:
    """Sweeps every ordered pair of statuses against the hand-written expectation."""
    permitted = target is current or target in EXPECTED[current]

    if permitted:
        validate_transition(current, target)
    else:
        with pytest.raises(InvalidRunStateError, match="Cannot move a run"):
            validate_transition(current, target)
    assert is_allowed(current, target) is permitted


@pytest.mark.parametrize("status", list(RunStatus))
def test_writing_the_same_state_twice_is_a_no_op(status: RunStatus) -> None:
    validate_transition(status, status)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.INSPECTED),
        (S.INSPECTED, S.AUDIO_READY),
        (S.AUDIO_READY, S.TRANSCRIBED),
        (S.TRANSCRIBED, S.ANALYZED),
        (S.ANALYZED, S.READY_FOR_REVIEW),
        (S.READY_FOR_REVIEW, S.REVIEWED),
        (S.REVIEWED, S.RENDERED),
        (S.RENDERED, S.COMPLETED),
    ],
)
def test_the_pipeline_advances_one_stage_at_a_time(current: RunStatus, target: RunStatus) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.AUDIO_READY),
        (S.CREATED, S.TRANSCRIBED),
        (S.CREATED, S.COMPLETED),
        (S.INSPECTED, S.TRANSCRIBED),
        (S.AUDIO_READY, S.READY_FOR_REVIEW),
        (S.AUDIO_READY, S.RENDERED),
        (S.TRANSCRIBED, S.REVIEWED),
        (S.ANALYZED, S.RENDERED),
        (S.ANALYZED, S.REVIEWED),
        (S.READY_FOR_REVIEW, S.RENDERED),
    ],
)
def test_stage_skips_are_rejected(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(InvalidRunStateError):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.COMPLETED, S.CREATED),
        (S.COMPLETED, S.RENDERED),
        (S.RENDERED, S.REVIEWED),
        (S.READY_FOR_REVIEW, S.ANALYZED),
        (S.REVIEWED, S.ANALYZED),
        (S.TRANSCRIBED, S.AUDIO_READY),
        (S.AUDIO_READY, S.INSPECTED),
        (S.INSPECTED, S.CREATED),
        (S.FAILED_INSPECT, S.CREATED),
        (S.FAILED_TRANSCRIPTION, S.AUDIO_READY),
    ],
)
def test_backwards_transitions_are_rejected(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(InvalidRunStateError):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("prerequisite", "failure"),
    [
        (S.CREATED, S.FAILED_INSPECT),
        (S.INSPECTED, S.FAILED_AUDIO),
        (S.AUDIO_READY, S.FAILED_TRANSCRIPTION),
        (S.TRANSCRIBED, S.FAILED_ANALYSIS),
        (S.ANALYZED, S.FAILED_PREVIEW),
        (S.READY_FOR_REVIEW, S.FAILED_REVIEW),
        (S.REVIEWED, S.FAILED_RENDER),
    ],
)
def test_a_stage_fails_from_the_state_that_precedes_it(
    prerequisite: RunStatus, failure: RunStatus
) -> None:
    validate_transition(prerequisite, failure)


@pytest.mark.parametrize(
    ("success", "failure"),
    [
        (S.INSPECTED, S.FAILED_INSPECT),
        (S.AUDIO_READY, S.FAILED_AUDIO),
        (S.TRANSCRIBED, S.FAILED_TRANSCRIPTION),
        (S.ANALYZED, S.FAILED_ANALYSIS),
        (S.READY_FOR_REVIEW, S.FAILED_PREVIEW),
        (S.REVIEWED, S.FAILED_REVIEW),
        (S.RENDERED, S.FAILED_RENDER),
    ],
)
def test_rerunning_a_completed_stage_may_fail(success: RunStatus, failure: RunStatus) -> None:
    validate_transition(success, failure)


@pytest.mark.parametrize(
    ("failure", "success"),
    [
        (S.FAILED_INSPECT, S.INSPECTED),
        (S.FAILED_AUDIO, S.AUDIO_READY),
        (S.FAILED_TRANSCRIPTION, S.TRANSCRIBED),
        (S.FAILED_ANALYSIS, S.ANALYZED),
        (S.FAILED_PREVIEW, S.READY_FOR_REVIEW),
        (S.FAILED_REVIEW, S.REVIEWED),
        (S.FAILED_RENDER, S.RENDERED),
    ],
)
def test_every_failure_can_be_retried_or_fail_again(failure: RunStatus, success: RunStatus) -> None:
    validate_transition(failure, success)
    validate_transition(failure, failure)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.FAILED_AUDIO),
        (S.CREATED, S.FAILED_TRANSCRIPTION),
        (S.CREATED, S.FAILED_RENDER),
        (S.INSPECTED, S.FAILED_TRANSCRIPTION),
        (S.AUDIO_READY, S.FAILED_INSPECT),
        (S.AUDIO_READY, S.FAILED_RENDER),
        (S.FAILED_INSPECT, S.FAILED_AUDIO),
        (S.FAILED_AUDIO, S.FAILED_TRANSCRIPTION),
        (S.TRANSCRIBED, S.FAILED_PREVIEW),
        (S.ANALYZED, S.FAILED_REVIEW),
        (S.FAILED_PREVIEW, S.FAILED_REVIEW),
        (S.COMPLETED, S.FAILED_RENDER),
    ],
)
def test_a_run_cannot_fail_in_a_stage_it_is_not_executing(
    current: RunStatus, target: RunStatus
) -> None:
    with pytest.raises(InvalidRunStateError):
        validate_transition(current, target)


def test_completed_is_terminal() -> None:
    for target in RunStatus:
        if target is not S.COMPLETED:
            with pytest.raises(InvalidRunStateError):
                validate_transition(S.COMPLETED, target)


@pytest.mark.parametrize(("stage", "outcomes"), sorted(EXPECTED_OUTCOMES.items()))
def test_each_stage_maps_to_the_outcomes_the_adr_declares(
    stage: RunStage, outcomes: tuple[RunStatus, RunStatus]
) -> None:
    success, failure = outcomes
    assert success_status(stage) is success
    assert failure_status(stage) is failure
    assert stage_for_failure(failure) is stage


def test_the_declared_stages_are_exactly_the_ones_in_the_adr() -> None:
    assert set(RunStage) == set(EXPECTED_OUTCOMES)


def test_reopening_a_review_is_the_only_backwards_edge() -> None:
    """ADR-030. Discarding human decisions is the one thing that walks back.

    Asserted as an exhaustive property rather than as a single allowed pair,
    so a second backwards edge added anywhere in the machine fails here.
    """
    order = list(SUCCESS_PATH)
    backwards = {
        (current, target)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
        if current in order and target in order and order.index(target) < order.index(current)
    }
    assert backwards == {(S.REVIEWED, S.READY_FOR_REVIEW)}
    assert frozenset(backwards) == RE_ENTRY_TRANSITIONS


def test_stage_for_failure_rejects_a_success_state() -> None:
    with pytest.raises(ValueError, match="not a failure state"):
        stage_for_failure(S.TRANSCRIBED)


def test_the_error_names_what_was_allowed_instead() -> None:
    """A refusal has to be actionable, not just a refusal."""
    with pytest.raises(InvalidRunStateError) as raised:
        validate_transition(S.CREATED, S.COMPLETED)

    message = str(raised.value)
    assert "CREATED" in message
    assert "COMPLETED" in message
    assert "INSPECTED" in message
    assert "FAILED_INSPECT" in message
