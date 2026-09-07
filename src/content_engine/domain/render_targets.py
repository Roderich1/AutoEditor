"""What a completed review asks the renderer to produce.

The one step in the engine where a person's judgement becomes a list of
intervals. Everything upstream of it is the machine's opinion and everything
downstream is an encoder following orders, so this is where an error would
either publish something somebody rejected or quietly replace an edit with the
boundaries they moved away from.

Three rules, and they are the whole of it:

- **an approval renders the candidate's own interval.** The decision carries
  ``final_start`` and ``final_end`` too, and the model already refuses an
  approval whose bounds moved, but the interval used here is the candidate's,
  so a decision file that somehow disagreed could not quietly become the source
  of truth for what gets cut.
- **an edit renders ``final_start`` and ``final_end`` and nothing else.** Not
  widened back to the analyzer's minimum duration, not re-snapped to a segment
  edge, not clamped to the proposal. A person watched the preview and decided
  where their clip ends.
- **a rejection renders nothing at all.**

And one refusal: a review that is not finished does not render. A candidate
with no decision is not a candidate the person chose to leave out -- it is one
they have not reached -- and rendering it either way would invent an answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from content_engine.domain.candidates import ValidatedCandidate
from content_engine.domain.enums import ReviewDecisionType
from content_engine.domain.exceptions import IncompatibleArtifactError
from content_engine.domain.review import (
    ReviewDecisionCollection,
    decisions_coherence_problem,
    pending_candidates,
)

__all__ = ["RenderTarget", "RenderTargetClip", "build_render_target"]


@dataclass(frozen=True)
class RenderTargetClip:
    """One clip to cut: which candidate, which decision, which seconds."""

    candidate: ValidatedCandidate
    decision: ReviewDecisionType
    start: float
    end: float
    #: Copied from the candidate rather than read through it. The candidate's
    #: rank is Optional because an unselected one has none, and narrowing that
    #: at every use site with an assertion would put a runtime check in the
    #: middle of a value object. ``build_render_target`` refuses an unranked
    #: candidate before constructing one of these, so the type is honest here.
    rank: int

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def original_start(self) -> float:
        return self.candidate.start

    @property
    def original_end(self) -> float:
        return self.candidate.end


@dataclass(frozen=True)
class RenderTarget:
    """The whole render request, and the identity of everything behind it.

    The identity fields are carried together with the clips rather than looked
    up again later, so the index that gets written, the fingerprint that gets
    recorded and the coherence check that reads them back all work from one
    object. Two places computing "which analysis was this" is two places that
    can disagree.
    """

    clips: tuple[RenderTargetClip, ...]
    analysis_fingerprint: str
    review_fingerprint: str
    decisions_sha256: str
    transcript_sha256: str
    source_sha256: str
    source_duration_seconds: float


def build_render_target(
    candidates: tuple[ValidatedCandidate, ...],
    decisions: ReviewDecisionCollection,
    analysis_fingerprint: str,
    review_fingerprint: str,
    decisions_sha256: str,
    transcript_sha256: str,
    source_sha256: str,
    source_duration_seconds: float,
) -> RenderTarget:
    """Turn a finished review into an ordered list of intervals to cut.

    Ordered by rank, deterministically, and independently of the order the
    decisions were taken in -- somebody who reviewed the list backwards, or
    resumed a session in the middle, must get the same clips in the same order
    as somebody who went straight through.
    """
    problem = decisions_coherence_problem(
        decisions, candidates, analysis_fingerprint, source_duration_seconds
    )
    if problem is not None:
        raise IncompatibleArtifactError(
            f"The decisions do not describe this run: {problem}. Complete the review, or "
            "start it again with `review RUN_ID --force`."
        )

    pending = pending_candidates(candidates, decisions)
    if pending:
        raise IncompatibleArtifactError(
            f"{len(pending)} of {len(candidates)} candidates have no decision, so the review "
            "is not finished and there is nothing settled to render. Run "
            "`content-engine review RUN_ID` and decide the rest."
        )

    taken = decisions.by_candidate
    clips = []
    # Rank order, taken from the candidates rather than from the decisions. The
    # shortlist is already ranked and the decision file is in the order somebody
    # happened to answer in, which is not an order at all.
    for candidate in sorted(candidates, key=_rank_of):
        decision = taken[candidate.id]
        interval = decision.final_interval
        if interval is None:
            continue
        # No bound check on `end` here. `ReviewDecisionCollection` refuses a
        # final interval past the duration it declares, and the coherence check
        # above refuses a collection whose declared duration is not this run's,
        # so an interval reaching past the source cannot arrive. A third check
        # would be unreachable code wearing the costume of a safeguard.
        start, end = interval
        if decision.decision is ReviewDecisionType.APPROVED:
            # The candidate's own interval, not the decision's copy of it. The
            # model already refuses an approval whose bounds moved, so the two
            # agree; taking them from the candidate means a decision file could
            # never quietly become the authority on what gets cut.
            start, end = candidate.start, candidate.end
        clips.append(
            RenderTargetClip(
                candidate=candidate,
                decision=decision.decision,
                start=start,
                end=end,
                rank=_rank_of(candidate),
            )
        )
    return RenderTarget(
        clips=tuple(clips),
        analysis_fingerprint=analysis_fingerprint,
        review_fingerprint=review_fingerprint,
        decisions_sha256=decisions_sha256,
        transcript_sha256=transcript_sha256,
        source_sha256=source_sha256,
        source_duration_seconds=source_duration_seconds,
    )


def _rank_of(candidate: ValidatedCandidate) -> int:
    """The rank a selected candidate must have, or a refusal.

    ``ValidatedCandidate`` refuses a rank on anything that was not selected, so
    a selected candidate always has one -- but the field is optional and the
    alternative to checking is sorting a list of ``None`` values, which raises
    somewhere far less informative than here.
    """
    if candidate.rank is None:
        raise IncompatibleArtifactError(
            f"Candidate {candidate.id} has no rank, so it was never selected and must not be "
            "rendered."
        )
    return candidate.rank
