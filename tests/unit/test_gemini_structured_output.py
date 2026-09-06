"""CE-029: what the provider is allowed to return, and what it is not.

This is the first of the two validation layers. It owns the shape of the answer
— that it is JSON, that it is an object with a candidate list, that a category
is one this build knows, that a score is an integer in range, that no total
score is supplied and that no unexpected field is smuggled in.

It deliberately does **not** own timestamps. A negative start, an inverted
interval and a timestamp outside the chunk are all things a real model produces,
and CE-030 exists to refuse them *with a recorded reason* in an artifact a human
can read. Rejecting them here would replace a measurement with a parse error and
lose the evidence, so the tests below assert that they pass through.

``NaN`` and the infinities are the exception, and the distinction is the point:
an impossible timestamp is data about the prompt, while a non-number is not a
timestamp at all and cannot be measured, snapped or ordered.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from content_engine.adapters.analysis.structured_output import (
    ProviderResponse,
    parse_provider_response,
)

from content_engine.domain.enums import ClipCategory
from content_engine.domain.exceptions import EXIT_ANALYSIS, AnalysisError


def scores(**overrides: int) -> dict[str, int]:
    base = {
        "hook": 80,
        "value": 75,
        "context_independence": 70,
        "clarity": 85,
        "engagement_potential": 60,
        "relevance": 90,
    }
    base.update(overrides)
    return base


def candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "start": 10.0,
        "end": 45.0,
        "category": "problem_solution",
        "topic": "Permisos de archivos en Linux",
        "hook": "Este error me costó una hora",
        "summary": "Explica por qué chmod no bastaba y qué faltaba.",
        "reason": "Problema concreto seguido de una solución verificable.",
        "scores": scores(),
    }
    base.update(overrides)
    return base


def payload(*candidates: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(candidates)}, ensure_ascii=False)


# --- what must be accepted ---------------------------------------------------


def test_a_response_with_no_candidates_is_a_valid_answer() -> None:
    """A chunk with nothing worth clipping must be sayable without failing."""
    assert parse_provider_response(payload()) == ()


def test_one_candidate_is_converted_to_the_domain_type() -> None:
    parsed = parse_provider_response(payload(candidate()))
    assert len(parsed) == 1
    only = parsed[0]
    assert only.start == 10.0
    assert only.end == 45.0
    assert only.category is ClipCategory.PROBLEM_SOLUTION
    assert only.topic == "Permisos de archivos en Linux"
    assert only.scores.hook == 80
    assert only.scores.relevance == 90
    assert only.warnings == []


def test_several_candidates_keep_the_order_the_provider_used() -> None:
    """Ordinals are assigned from this order, and they enter the identity."""
    parsed = parse_provider_response(
        payload(
            candidate(topic="primero"),
            candidate(topic="segundo"),
            candidate(topic="tercero"),
        )
    )
    assert [item.topic for item in parsed] == ["primero", "segundo", "tercero"]


def test_every_category_the_domain_knows_is_accepted() -> None:
    for member in ClipCategory:
        parsed = parse_provider_response(payload(candidate(category=member.value)))
        assert parsed[0].category is member


def test_the_score_bounds_are_inclusive() -> None:
    parsed = parse_provider_response(payload(candidate(scores=scores(hook=0, relevance=100))))
    assert parsed[0].scores.hook == 0
    assert parsed[0].scores.relevance == 100


# --- timestamps belong to CE-030, not here -----------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (-5.0, 30.0),  # negative
        (60.0, 20.0),  # inverted
        (30.0, 30.0),  # zero length
        (99999.0, 99999.5),  # far outside any chunk
    ],
)
def test_impossible_intervals_reach_the_domain_instead_of_failing_here(
    start: float, end: float
) -> None:
    parsed = parse_provider_response(payload(candidate(start=start, end=end)))
    assert (parsed[0].start, parsed[0].end) == (start, end)


# --- what must be refused ----------------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_timestamp_is_refused(literal: str) -> None:
    """Not a timestamp at all, so there is nothing for CE-030 to measure."""
    text = '{"candidates": [{"start": LITERAL, "end": 45.0, "category": "tip", '
    text += '"topic": "t", "hook": "h", "summary": "s", "reason": "r", "scores": '
    text += json.dumps(scores()) + "}]}"
    text = text.replace("LITERAL", literal)
    with pytest.raises(AnalysisError):
        parse_provider_response(text)


def test_a_missing_field_is_refused() -> None:
    incomplete = candidate()
    del incomplete["summary"]
    text = payload(incomplete)
    with pytest.raises(AnalysisError, match="summary"):
        parse_provider_response(text)


def test_an_extra_field_is_refused() -> None:
    text = payload(candidate(confidence=0.9))
    with pytest.raises(AnalysisError, match="confidence"):
        parse_provider_response(text)


def test_a_total_score_is_refused_rather_than_ignored() -> None:
    """ADR-008. Silently dropping it would hide a prompt that asked for it."""
    text = payload(candidate(total_score=91.5))
    with pytest.raises(AnalysisError, match="total_score"):
        parse_provider_response(text)


def test_an_unknown_category_is_refused() -> None:
    text = payload(candidate(category="rant"))
    with pytest.raises(AnalysisError, match="category"):
        parse_provider_response(text)


@pytest.mark.parametrize("value", [-1, 101, 1000])
def test_a_score_outside_the_range_is_refused(value: int) -> None:
    text = payload(candidate(scores=scores(clarity=value)))
    with pytest.raises(AnalysisError, match="clarity"):
        parse_provider_response(text)


def test_a_missing_score_is_refused() -> None:
    partial = scores()
    del partial["engagement_potential"]
    text = payload(candidate(scores=partial))
    with pytest.raises(AnalysisError, match="engagement_potential"):
        parse_provider_response(text)


def test_an_empty_response_is_refused() -> None:
    with pytest.raises(AnalysisError, match="empty"):
        parse_provider_response("")


def test_a_whitespace_only_response_is_refused() -> None:
    with pytest.raises(AnalysisError, match="empty"):
        parse_provider_response("   \n  ")


def test_truncated_json_is_refused() -> None:
    text = payload(candidate())[:-12]
    with pytest.raises(AnalysisError, match="not valid JSON"):
        parse_provider_response(text)


def test_prose_instead_of_json_is_refused() -> None:
    with pytest.raises(AnalysisError, match="not valid JSON"):
        parse_provider_response("Lo siento, no puedo ayudar con eso.")


@pytest.mark.parametrize("text", ["[]", '"candidates"', "42", "null", "true"])
def test_a_response_that_is_not_an_object_is_refused(text: str) -> None:
    with pytest.raises(AnalysisError, match="object"):
        parse_provider_response(text)


def test_a_candidate_list_holding_a_non_object_is_refused() -> None:
    text = json.dumps({"candidates": ["no soy un candidato"]})
    with pytest.raises(AnalysisError):
        parse_provider_response(text)


def test_one_invalid_candidate_refuses_the_whole_batch() -> None:
    """Half a batch is not a smaller answer; it is an answer nobody gave."""
    text = payload(candidate(), candidate(category="rant"), candidate())
    with pytest.raises(AnalysisError):
        parse_provider_response(text)


def test_a_missing_candidate_list_is_refused() -> None:
    with pytest.raises(AnalysisError, match="candidates"):
        parse_provider_response("{}")


# --- the failure is an analysis failure, with the analysis exit code ---------


def test_every_refusal_carries_the_analysis_exit_code() -> None:
    with pytest.raises(AnalysisError) as caught:
        parse_provider_response("no")
    assert caught.value.exit_code == EXIT_ANALYSIS


# --- the schema handed to the provider ---------------------------------------


def test_the_response_model_forbids_unknown_fields_at_every_level() -> None:
    assert ProviderResponse.model_config["extra"] == "forbid"
    assert ProviderResponse.model_config["allow_inf_nan"] is False


def test_the_schema_names_the_categories_and_bounds_the_scores() -> None:
    """What is sent to the provider, so the constraints are enforced twice."""
    schema = json.dumps(ProviderResponse.model_json_schema())
    for member in ClipCategory:
        assert member.value in schema
    assert "total_score" not in schema
