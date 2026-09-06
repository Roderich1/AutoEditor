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

The schema *sent* to the provider is built from these same models, field for
field, but it is not the models themselves. Handing Pydantic straight to the SDK
produced a request the API rejects outright with a 400: ``extra="forbid"``
becomes ``additionalProperties: false``, which is not part of the schema subset
``generateContent`` accepts. It also swept every internal docstring in this
module into ``description`` fields and shipped them to the model as part of the
request -- commentary about ``RawCandidate`` and about why ``warnings`` is
absent, sent to Gemini, paid for by the token.

So the request schema is assembled deliberately in ``provider_response_schema``
and a test asserts it lists exactly the fields these models declare. The
constraints are still stated twice, but only the ones the wire format supports
travel: types, the category enum, required fields and their ordering. Score
bounds and string lengths stay here, on the answer, which is the layer that has
to be trusted anyway -- a provider that ignored them would be caught either way,
and the prompt already states the 0-100 range in words.
"""

from __future__ import annotations

import json
from typing import Any

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


#: JSON types for the fields of one candidate, in the order they are requested.
#: Values are the wire types `generateContent` accepts, which is a subset of
#: OpenAPI and notably excludes `additionalProperties`.
_CANDIDATE_TYPES: dict[str, str] = {
    "start": "NUMBER",
    "end": "NUMBER",
    "category": "STRING",
    "topic": "STRING",
    "hook": "STRING",
    "summary": "STRING",
    "reason": "STRING",
    "scores": "OBJECT",
}


def provider_response_schema() -> dict[str, Any]:
    """The response schema sent to the provider, in the shape the API accepts.

    Derived from the models above rather than written twice: the field names and
    the category values come from them, so a field added to `ProviderCandidate`
    without being considered here fails the test that compares the two.

    What is deliberately *not* included is anything the wire format rejects or
    nobody asked for. `additionalProperties` is absent because the API refuses a
    request containing it. Descriptions are absent because the only descriptions
    available are this module's own docstrings, which are notes to maintainers
    and have no business being sent to a model.
    """
    scores = {
        "type": "OBJECT",
        "properties": {name: {"type": "INTEGER"} for name in ProviderScores.model_fields},
        "required": list(ProviderScores.model_fields),
        "propertyOrdering": list(ProviderScores.model_fields),
    }
    candidate: dict[str, Any] = {
        "type": "OBJECT",
        "properties": {
            name: (scores if kind == "OBJECT" else {"type": kind})
            for name, kind in _CANDIDATE_TYPES.items()
        },
        "required": list(_CANDIDATE_TYPES),
        "propertyOrdering": list(_CANDIDATE_TYPES),
    }
    candidate["properties"]["category"] = {
        "type": "STRING",
        "enum": [member.value for member in ClipCategory],
    }
    return {
        "type": "OBJECT",
        "properties": {"candidates": {"type": "ARRAY", "items": candidate}},
        "required": ["candidates"],
        "propertyOrdering": ["candidates"],
    }


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
            "The provider returned an empty response. Nothing was proposed and no reason was given."
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
