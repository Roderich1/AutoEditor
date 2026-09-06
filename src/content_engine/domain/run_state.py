"""Explicit run state machine.

A run walks forward through the pipeline or stops at the failure state of the
stage that broke. Arbitrary backwards transitions and stage skips are rejected,
so a manifest can never claim a run reached a state it did not earn.
"""

from __future__ import annotations

from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import InvalidRunStateError

#: The linear success path. Each entry is the only forward move allowed.
SUCCESS_PATH: tuple[RunStatus, ...] = (
    RunStatus.CREATED,
    RunStatus.INSPECTED,
    RunStatus.AUDIO_READY,
    RunStatus.TRANSCRIBED,
    RunStatus.ANALYZED,
    RunStatus.READY_FOR_REVIEW,
    RunStatus.REVIEWED,
    RunStatus.RENDERED,
    RunStatus.COMPLETED,
)

#: Which stage produces which success state, and therefore which failure state.
STAGE_OUTCOMES: dict[RunStage, tuple[RunStatus, RunStatus]] = {
    RunStage.INSPECT: (RunStatus.INSPECTED, RunStatus.FAILED_INSPECT),
    RunStage.AUDIO: (RunStatus.AUDIO_READY, RunStatus.FAILED_AUDIO),
    RunStage.TRANSCRIPTION: (RunStatus.TRANSCRIBED, RunStatus.FAILED_TRANSCRIPTION),
    RunStage.ANALYSIS: (RunStatus.ANALYZED, RunStatus.FAILED_ANALYSIS),
    RunStage.PREVIEW: (RunStatus.READY_FOR_REVIEW, RunStatus.FAILED_PREVIEW),
    RunStage.REVIEW: (RunStatus.REVIEWED, RunStatus.FAILED_REVIEW),
    RunStage.RENDER: (RunStatus.RENDERED, RunStatus.FAILED_RENDER),
}

#: Backwards moves the machine allows, and the only ones. ADR-030.
#:
#: Every other stage owns artifacts it derives from its inputs, so re-running it
#: needs no backwards edge: the state it produces is the state it produced
#: before. Review is the exception, because what it holds is not derived from
#: anything -- it is a set of human decisions, and `review --force` discards
#: them. A run whose decisions have just been thrown away must not go on
#: claiming REVIEWED while the person works through the list again, and there is
#: no honest state for it other than the one it came from.
#:
#: Written as an explicit pair rather than a general "one step back" rule. A
#: general rule would also permit READY_FOR_REVIEW -> ANALYZED and
#: REVIEWED -> READY_FOR_REVIEW for reasons nobody decided on.
RE_ENTRY_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {(RunStatus.REVIEWED, RunStatus.READY_FOR_REVIEW)}
)


def _predecessor(status: RunStatus) -> RunStatus:
    return SUCCESS_PATH[SUCCESS_PATH.index(status) - 1]


def stage_for_failure(status: RunStatus) -> RunStage:
    for stage, (_, failure) in STAGE_OUTCOMES.items():
        if failure is status:
            return stage
    raise ValueError(f"{status} is not a failure state")


def failure_status(stage: RunStage) -> RunStatus:
    return STAGE_OUTCOMES[stage][1]


def success_status(stage: RunStage) -> RunStatus:
    return STAGE_OUTCOMES[stage][0]


def _build_transitions() -> dict[RunStatus, frozenset[RunStatus]]:
    transitions: dict[RunStatus, set[RunStatus]] = {status: set() for status in RunStatus}

    # Forward along the success path, one stage at a time.
    for current, following in zip(SUCCESS_PATH, SUCCESS_PATH[1:], strict=False):
        transitions[current].add(following)

    for stage, (success, failure) in STAGE_OUTCOMES.items():
        prerequisite = _predecessor(success)
        # A stage may fail from the state that precedes it.
        transitions[prerequisite].add(failure)
        # Re-running a completed stage may also fail, and that must be recordable.
        transitions[success].add(failure)
        # A retry either succeeds or fails again.
        transitions[failure].add(success)
        transitions[failure].add(failure)
        del stage

    for current, target in RE_ENTRY_TRANSITIONS:
        transitions[current].add(target)

    return {status: frozenset(targets) for status, targets in transitions.items()}


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = _build_transitions()


def is_allowed(current: RunStatus, target: RunStatus) -> bool:
    """A no-op transition is allowed so writing the same state twice is safe."""
    return target is current or target in ALLOWED_TRANSITIONS[current]


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    if not is_allowed(current, target):
        allowed = ", ".join(sorted(ALLOWED_TRANSITIONS[current])) or "none"
        raise InvalidRunStateError(
            f"Cannot move a run from {current} to {target}. Allowed from {current}: {allowed}."
        )
