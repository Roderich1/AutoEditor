from __future__ import annotations

import pytest

from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import InvalidRunStateError
from content_engine.domain.run_state import (
    ALLOWED_TRANSITIONS,
    SUCCESS_PATH,
    failure_status,
    is_allowed,
    stage_for_failure,
    success_status,
    validate_transition,
)

FAILURE_STATES = {
    RunStatus.FAILED_INSPECT,
    RunStatus.FAILED_AUDIO,
    RunStatus.FAILED_TRANSCRIPTION,
    RunStatus.FAILED_ANALYSIS,
    RunStatus.FAILED_RENDER,
}


@pytest.mark.parametrize(
    ("current", "target"),
    list(zip(SUCCESS_PATH, SUCCESS_PATH[1:], strict=False)),
)
def test_success_path_moves_forward_one_stage_at_a_time(
    current: RunStatus, target: RunStatus
) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.CREATED, RunStatus.FAILED_INSPECT),
        (RunStatus.INSPECTED, RunStatus.FAILED_AUDIO),
        (RunStatus.AUDIO_READY, RunStatus.FAILED_TRANSCRIPTION),
        (RunStatus.TRANSCRIBED, RunStatus.FAILED_ANALYSIS),
        (RunStatus.REVIEWED, RunStatus.FAILED_RENDER),
    ],
)
def test_a_stage_may_fail_from_its_prerequisite(current: RunStatus, target: RunStatus) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize("stage", list(RunStage))
def test_rerunning_a_completed_stage_may_fail(stage: RunStage) -> None:
    validate_transition(success_status(stage), failure_status(stage))


@pytest.mark.parametrize("stage", list(RunStage))
def test_a_failed_stage_can_be_retried_or_fail_again(stage: RunStage) -> None:
    failure = failure_status(stage)
    validate_transition(failure, success_status(stage))
    validate_transition(failure, failure)


@pytest.mark.parametrize("status", list(RunStatus))
def test_writing_the_same_state_twice_is_a_no_op(status: RunStatus) -> None:
    validate_transition(status, status)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.COMPLETED, RunStatus.CREATED),
        (RunStatus.TRANSCRIBED, RunStatus.INSPECTED),
        (RunStatus.AUDIO_READY, RunStatus.CREATED),
        (RunStatus.RENDERED, RunStatus.REVIEWED),
        (RunStatus.FAILED_INSPECT, RunStatus.CREATED),
    ],
)
def test_arbitrary_backwards_transitions_are_rejected(
    current: RunStatus, target: RunStatus
) -> None:
    with pytest.raises(InvalidRunStateError, match="Cannot move a run"):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.CREATED, RunStatus.AUDIO_READY),
        (RunStatus.CREATED, RunStatus.COMPLETED),
        (RunStatus.INSPECTED, RunStatus.TRANSCRIBED),
        (RunStatus.AUDIO_READY, RunStatus.RENDERED),
    ],
)
def test_stage_skips_are_rejected(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(InvalidRunStateError):
        validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.CREATED, RunStatus.FAILED_AUDIO),
        (RunStatus.CREATED, RunStatus.FAILED_TRANSCRIPTION),
        (RunStatus.INSPECTED, RunStatus.FAILED_TRANSCRIPTION),
        (RunStatus.AUDIO_READY, RunStatus.FAILED_RENDER),
        (RunStatus.FAILED_INSPECT, RunStatus.FAILED_AUDIO),
    ],
)
def test_a_run_cannot_fail_in_a_stage_it_is_not_executing(
    current: RunStatus, target: RunStatus
) -> None:
    with pytest.raises(InvalidRunStateError):
        validate_transition(current, target)


def test_every_status_is_covered_by_the_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(RunStatus)


def test_terminal_states_have_no_outgoing_success_transitions() -> None:
    assert ALLOWED_TRANSITIONS[RunStatus.COMPLETED] == frozenset()


def test_the_full_matrix_matches_the_declared_table() -> None:
    """No transition is accepted that the table does not list."""
    for current in RunStatus:
        for target in RunStatus:
            expected = target is current or target in ALLOWED_TRANSITIONS[current]
            assert is_allowed(current, target) is expected


@pytest.mark.parametrize("stage", list(RunStage))
def test_failure_states_map_back_to_their_stage(stage: RunStage) -> None:
    assert stage_for_failure(failure_status(stage)) is stage


def test_stage_for_failure_rejects_a_success_state() -> None:
    with pytest.raises(ValueError, match="not a failure state"):
        stage_for_failure(RunStatus.TRANSCRIBED)


def test_failure_states_are_exactly_the_declared_ones() -> None:
    assert {failure_status(stage) for stage in RunStage} == FAILURE_STATES
