"""CE-029: the contract a provider's answer must satisfy, and nothing more.

This is the first of the two validation layers the specification requires. It
owns the *shape* of an answer: that it is JSON, that it is an object holding a
candidate list, that a category is one this build knows, that each score is an
integer in range, that no field was added and none omitted, and that no total
score was supplied.

It deliberately does not own timestamps. A negative start, an inverted interval
and a range outside the chunk are all things a real model produces, and CE-030
exists to refuse them *with a recorded reason* in an artifact a human can read.
Refusing them here would replace a measurement with a parse error and destroy
the evidence that the prompt needs work. So they pass through untouched.

``NaN`` and the infinities are the one exception, and the difference is the
whole point: an impossible timestamp is data about the prompt, while a
non-number is not a timestamp at all — nothing downstream can measure it, snap
it, order it or serialise it.

The same models are handed to the provider as the requested response schema, so
the constraints are stated once and enforced twice: once by the provider that
was asked to obey them, and once here on whatever actually came back.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from content_engine.domain.candidates import CandidateScores, RawCandidate
from content_engine.domain.enums import ClipCategory
from content_engine.domain.exceptions import AnalysisError

#: Bumped whenever the requested response shape changes. It is part of the
#: adapter version rather than an artifact field: the artifacts record what came
#: out of the domain, not what the provider was asked for.
RESPONSE_SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProviderScores(_Strict):
    """The six dimensions, bounded. No total: ADR-008 computes it in Python."""

    hook: int = Field(ge=0, le=100)
    value: int = Field(ge=0, le=100)
    context_independence: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)
    engagement_potential: int = Field(ge=0, le=100)
    relevance: int = Field(ge=0, le=100)


class ProviderCandidate(_Strict):
    """One proposal as the provider stated it.

    ``start`` and ``end`` carry no ordering or sign constraint, matching
    ``RawCandidate``. The text lengths do match the domain's, so a provider that
    returns a two-thousand-word summary is refused here with a message naming
    the field rather than inside the conversion below.

    ``warnings`` is absent on purpose. The domain has the field, but nothing is
    asking the provider to fill it, and ``extra="forbid"`` means a model that
    invents it is refused rather than quietly obeyed.
    """

    start: float
    end: float
    category: ClipCategory
    topic: str = Field(min_length=1, max_length=200)
    hook: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    scores: ProviderScores


class ProviderResponse(_Strict):
    """The whole answer: a list, possibly empty."""

    candidates: list[ProviderCandidate] = Field(max_length=200)


def parse_provider_response(text: str) -> tuple[RawCandidate, ...]:
    """Turn a provider's raw text into domain proposals, or fail as analysis.

    Every failure here is an ``AnalysisError`` and therefore exit 5: the stage
    ran, a provider was reached, and what came back could not be used. That is a
    different thing from a missing credential (exit 2) or an artifact on disk
    that no longer matches its inputs (exit 3), and the exit code is how a
    caller tells them apart without reading the message.

    A single unusable candidate refuses the whole batch. Silently dropping it
    would report a shorter answer as though the provider had given one, and the
    count of proposals is the denominator of every quality metric this project
    has.
    """
    if not text.strip():
        raise AnalysisError(
            "The provider returned an empty response. Nothing was proposed and no "
            "reason was given."
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise AnalysisError(
            f"The provider returned text that is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise AnalysisError(
            f"The provider returned a JSON {type(payload).__name__}, not an object with "
            "a candidate list."
        )
    try:
        response = ProviderResponse.model_validate(payload)
    except ValidationError as error:
        raise AnalysisError(
            f"The provider's answer does not match the requested schema: {error}"
        ) from error
    return tuple(_to_domain(candidate) for candidate in response.candidates)


def _to_domain(candidate: ProviderCandidate) -> RawCandidate:
    """Copy a validated proposal into the domain type, adding nothing.

    Field by field rather than by unpacking, so a field added to either model
    without being considered here is a type error rather than a value that
    silently crosses the boundary.
    """
    return RawCandidate(
        start=candidate.start,
        end=candidate.end,
        category=candidate.category,
        topic=candidate.topic,
        hook=candidate.hook,
        summary=candidate.summary,
        reason=candidate.reason,
        scores=CandidateScores(
            hook=candidate.scores.hook,
            value=candidate.scores.value,
            context_independence=candidate.scores.context_independence,
            clarity=candidate.scores.clarity,
            engagement_potential=candidate.scores.engagement_potential,
            relevance=candidate.scores.relevance,
        ),
        warnings=[],
    )
