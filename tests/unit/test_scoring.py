"""Deterministic scoring (CE-025).

The total decides whether a candidate clears min_score, and min_score decides
whether a human ever sees it. A total that differs by a hundredth between two
machines makes two runs of the same experiment incomparable, which is the thing
ADR-015 exists to prevent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from content_engine.domain.candidates import CandidateScores
from content_engine.domain.scoring import SCORE_FORMULA_VERSION, WEIGHTS, calculate_total


def _scores(**overrides: int) -> CandidateScores:
    payload: dict[str, int] = {
        "hook": 0,
        "value": 0,
        "context_independence": 0,
        "clarity": 0,
        "engagement_potential": 0,
        "relevance": 0,
    }
    payload.update(overrides)
    return CandidateScores(**payload)


def test_the_weights_are_the_ones_the_adr_declares() -> None:
    """Written out rather than read from WEIGHTS: the ADR is the authority."""
    assert {
        "hook": Decimal("0.25"),
        "value": Decimal("0.20"),
        "context_independence": Decimal("0.20"),
        "clarity": Decimal("0.15"),
        "engagement_potential": Decimal("0.10"),
        "relevance": Decimal("0.10"),
    } == WEIGHTS


def test_the_weights_sum_to_exactly_one() -> None:
    """Anything else would make the 0-100 range a lie at one end or the other."""
    assert sum(WEIGHTS.values()) == Decimal("1.00")


def test_a_candidate_scored_zero_everywhere_totals_zero() -> None:
    assert calculate_total(_scores()) == 0.0


def test_a_candidate_scored_one_hundred_everywhere_totals_one_hundred() -> None:
    perfect = _scores(
        hook=100,
        value=100,
        context_independence=100,
        clarity=100,
        engagement_potential=100,
        relevance=100,
    )

    assert calculate_total(perfect) == 100.0


def test_the_worked_example_from_the_specification() -> None:
    """SPEC section 24: these six ratings total 91.15."""
    scores = _scores(
        hook=92,
        value=88,
        context_independence=96,
        clarity=93,
        engagement_potential=84,
        relevance=90,
    )

    assert calculate_total(scores) == 91.15


@pytest.mark.parametrize("dimension", list(WEIGHTS))
def test_every_dimension_moves_the_total(dimension: str) -> None:
    baseline = calculate_total(_scores())

    assert calculate_total(_scores(**{dimension: 100})) > baseline


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        ("hook", 25.0),
        ("value", 20.0),
        ("context_independence", 20.0),
        ("clarity", 15.0),
        ("engagement_potential", 10.0),
        ("relevance", 10.0),
    ],
)
def test_each_dimension_contributes_exactly_its_weight(dimension: str, expected: float) -> None:
    assert calculate_total(_scores(**{dimension: 100})) == expected


def test_integer_ratings_never_need_rounding_at_all() -> None:
    """Honest statement of what the Decimal buys.

    Every weight is a multiple of 0.05, so every reachable total is already exact
    at two decimals and the quantisation is a no-op today. The rule is declared
    anyway so a future weight change cannot silently start rounding to even.
    """
    exact = sum(
        (
            Decimal(value) * weight
            for value, weight in zip([92, 88, 96, 93, 84, 90], WEIGHTS.values(), strict=True)
        ),
        start=Decimal(0),
    )

    assert exact == Decimal("91.15")
    assert exact == exact.quantize(Decimal("0.01"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.005", "0.01"), ("0.015", "0.02"), ("0.025", "0.03"), ("2.675", "2.68")],
)
def test_the_declared_rounding_rule_is_half_up_not_half_to_even(value: str, expected: str) -> None:
    """round() would give 0.02 for 0.025 and 2.67 for 2.675; this must not."""
    assert Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal(expected)


def test_the_total_is_stable_across_independent_constructions() -> None:
    first = calculate_total(
        CandidateScores(
            hook=92,
            value=88,
            context_independence=96,
            clarity=93,
            engagement_potential=84,
            relevance=90,
        )
    )
    second = calculate_total(
        CandidateScores(
            relevance=90,
            engagement_potential=84,
            clarity=93,
            context_independence=96,
            value=88,
            hook=92,
        )
    )

    assert first == second


@pytest.mark.parametrize("hook", list(range(0, 101, 7)))
def test_the_total_never_leaves_the_declared_range(hook: int) -> None:
    total = calculate_total(_scores(hook=hook, value=hook, clarity=hook))

    assert 0.0 <= total <= 100.0


def test_the_total_is_quantised_to_two_decimals() -> None:
    total = calculate_total(
        _scores(hook=37, value=41, context_independence=53, clarity=67, relevance=71)
    )

    assert Decimal(str(total)).as_tuple().exponent >= -2


def test_the_formula_version_is_declared() -> None:
    """A total is comparable only against another produced the same way."""
    assert SCORE_FORMULA_VERSION == 1
