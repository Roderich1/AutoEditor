"""Orchestration of the review stage (CE-035 to CE-039).

Holds the decisions a person has taken and the rules about when they may be
read back, resumed or replaced. It knows nothing about prompts, keys or
terminals: the CLI asks the questions, this decides what an answer means and
puts it somewhere durable.

The one property everything here exists to protect is that **a decision, once
taken, survives whatever happens next**. Every explicit answer is written
before the next candidate is shown, through the same atomic write the rest of
the engine uses, so a crash, a closed terminal or a Ctrl+C between two
candidates costs nothing that was already decided. That is why the session
saves per decision rather than at the end: the alternative asks somebody to
redo twenty minutes of judgement because of a lost SSH connection.

The counterpart is that nothing is ever written on somebody's behalf. A skipped
candidate has no entry, an unopened session writes no file, and the run only
reaches REVIEWED when every selected candidate has an explicit decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from content_engine.domain.candidates import CANDIDATES_SCHEMA_VERSION, ValidatedCandidate
from content_engine.domain.exceptions import IncompatibleArtifactError
from content_engine.domain.review import (
    DECISIONS_SCHEMA_VERSION,
    REVIEW_RULES_VERSION,
    ApprovedDecision,
    EditedDecision,
    RejectedDecision,
    ReviewDecisionCollection,
    ReviewStageConfig,
    decisions_coherence_problem,
    pending_candidates,
    review_fingerprint,
    review_stage_config_sha256,
)
from content_engine.utils.json import read_json, write_json

DECISIONS_FILENAME = "decisions.json"
REVIEW_STAGE_CONFIG_FILENAME = "config.effective.json"

#: What one answer can produce. Named so the CLI never has to import the three
#: models to describe the type it hands back.
Decision = ApprovedDecision | RejectedDecision | EditedDecision


@dataclass(frozen=True)
class ReviewPlan:
    """The shortlist a session is about, and the identity it belongs to."""

    candidates: tuple[ValidatedCandidate, ...]
    analysis_fingerprint: str
    source_duration_seconds: float


def review_stage_config(plan: ReviewPlan) -> ReviewStageConfig:
    """What the review stage was asked about, in readable form."""
    return ReviewStageConfig(
        rules_version=REVIEW_RULES_VERSION,
        decisions_schema_version=DECISIONS_SCHEMA_VERSION,
        candidates_schema_version=CANDIDATES_SCHEMA_VERSION,
        analysis_fingerprint=plan.analysis_fingerprint,
        source_duration_seconds=plan.source_duration_seconds,
        selected_candidates=len(plan.candidates),
    )


def empty_collection(plan: ReviewPlan, now: datetime) -> ReviewDecisionCollection:
    return ReviewDecisionCollection(
        analysis_fingerprint=plan.analysis_fingerprint,
        source_duration_seconds=plan.source_duration_seconds,
        created_at=now,
        updated_at=now,
        decisions=[],
    )


def write_decisions(directory: Path, collection: ReviewDecisionCollection) -> None:
    """Persist the decisions taken so far, atomically."""
    write_json(directory.joinpath(DECISIONS_FILENAME), collection.model_dump(mode="json"))


def read_decisions(directory: Path) -> ReviewDecisionCollection:
    """Load the decisions on disk, or refuse them.

    A file that cannot be interpreted with certainty is refused rather than
    partially recovered. Salvaging what parses out of a damaged decision file
    would silently drop somebody's judgement, and a dropped rejection reappears
    as a candidate they are asked about again -- with no sign that the first
    answer ever existed.
    """
    path = directory.joinpath(DECISIONS_FILENAME)
    if not path.is_file():
        raise IncompatibleArtifactError(f"This run has no {DECISIONS_FILENAME}: {path}")
    try:
        payload = read_json(path)
    except Exception as error:  # noqa: BLE001 - every read failure is one refusal
        raise IncompatibleArtifactError(
            f"{path} cannot be read as review decisions: {error}. Use --force to start the "
            "review again, which discards them."
        ) from error
    if not isinstance(payload, dict):
        raise IncompatibleArtifactError(f"{path} does not contain a review decision collection.")
    declared = payload.get("schema_version")
    if declared != DECISIONS_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares decision schema {declared!r}; this build understands "
            f"{DECISIONS_SCHEMA_VERSION}. The decisions were produced by a different version "
            "and are not interpreted."
        )
    try:
        return ReviewDecisionCollection.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid review decision collection: {error}. Use --force to start "
            "the review again, which discards it."
        ) from error


def require_decisions(directory: Path, plan: ReviewPlan) -> ReviewDecisionCollection:
    """Load the decisions and prove they were taken over this shortlist."""
    collection = read_decisions(directory)
    problem = decisions_coherence_problem(
        collection,
        plan.candidates,
        plan.analysis_fingerprint,
        plan.source_duration_seconds,
    )
    if problem is not None:
        raise IncompatibleArtifactError(
            f"The decisions in {directory.joinpath(DECISIONS_FILENAME)} do not describe this "
            f"run: {problem}. Use --force to start the review again, which discards them."
        )
    return collection


class ReviewSession:
    """One pass over the pending candidates, saving after every answer.

    Deliberately not a pure object. Its whole purpose is the side effect: the
    caller records a decision and the file on disk is already updated when the
    call returns, so there is no window in which an answered candidate exists
    only in memory.
    """

    def __init__(
        self,
        directory: Path,
        plan: ReviewPlan,
        collection: ReviewDecisionCollection,
    ) -> None:
        self.directory = directory
        self.plan = plan
        self.collection = collection

    @property
    def pending(self) -> list[ValidatedCandidate]:
        return pending_candidates(self.plan.candidates, self.collection)

    @property
    def complete(self) -> bool:
        return not self.pending

    def record(self, decision: Decision, now: datetime) -> None:
        """Add one decision and write the collection, or change nothing.

        The new collection is built and validated before anything is written,
        so a decision the model refuses leaves the file holding the previous
        set rather than a document that fails to load next time.
        """
        replaced = [
            existing
            for existing in self.collection.decisions
            if existing.candidate_id != decision.candidate_id
        ]
        updated = ReviewDecisionCollection(
            analysis_fingerprint=self.collection.analysis_fingerprint,
            source_duration_seconds=self.collection.source_duration_seconds,
            created_at=self.collection.created_at,
            updated_at=now,
            decisions=[*replaced, decision],
        )
        self._persist(updated)

    def open(self, now: datetime) -> None:
        """Write the collection and the stage configuration as they stand.

        Called when a session has to exist on disk without a decision having
        been taken: a shortlist with nothing in it, whose review is complete
        the moment it starts, still has to leave the honest record that zero
        candidates were decided.
        """
        self._persist(self.collection.model_copy(update={"updated_at": now}))

    def _persist(self, collection: ReviewDecisionCollection) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json(
            self.directory.joinpath(REVIEW_STAGE_CONFIG_FILENAME),
            review_stage_config(self.plan).model_dump(mode="json"),
        )
        write_decisions(self.directory, collection)
        self.collection = collection

    def stage_record(self) -> tuple[str, str]:
        """The fingerprint and configuration digest of a finished review.

        Only meaningful once every selected candidate has been decided, which
        is why it refuses otherwise: a fingerprint over a half-finished session
        would be recorded in the manifest as a completed stage.
        """
        if not self.complete:
            raise IncompatibleArtifactError(
                f"{len(self.pending)} of {len(self.plan.candidates)} candidates have no "
                "decision, so the review is not finished and cannot be recorded."
            )
        config = review_stage_config(self.plan)
        return (
            review_fingerprint(self.plan.analysis_fingerprint, self.collection, config),
            review_stage_config_sha256(config),
        )
