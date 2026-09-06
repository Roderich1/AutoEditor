"""Deterministic candidate scoring (CE-025).

ADR-008: the model rates six dimensions, Python computes the total. That split
exists so the weights can be versioned and experimented with without touching a
prompt, and so a model cannot inflate its own result.

The arithmetic is done in ``Decimal`` with an explicit half-up rule. To be
accurate about why: with the current weights and integer ratings this is not
fixing a live bug. Every weight is a multiple of 0.05, so every total is exactly
representable at two decimals, the quantisation never actually rounds anything,
and binary floats happen to agree on all 101**6 possible inputs.

What ``Decimal`` buys is that the guarantee is structural rather than a property
of the numbers we chose. The total decides whether a candidate clears
``min_score``, and ``min_score`` decides whether a human ever sees it. The moment
a weight with three decimals is tried in an experiment, float accumulation order
and ``round()``'s ties-to-even become able to move a candidate across that line,
and the failure would be invisible: a slightly different shortlist, not an error.
Paying for exactness now costs nothing and removes that class of surprise from
the experiment matrix ahead.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from content_engine.domain.candidates import CandidateScores

#: Bumped whenever the weights or the rounding rule change, so a total can be
#: compared only against another produced the same way.
SCORE_FORMULA_VERSION = 1

#: ADR-008. Written as strings because Decimal("0.25") is exactly a quarter and
#: Decimal(0.25) is whatever the float happened to be.
WEIGHTS: dict[str, Decimal] = {
    "hook": Decimal("0.25"),
    "value": Decimal("0.20"),
    "context_independence": Decimal("0.20"),
    "clarity": Decimal("0.15"),
    "engagement_potential": Decimal("0.10"),
    "relevance": Decimal("0.10"),
}

_QUANTUM = Decimal("0.01")


def calculate_total(scores: CandidateScores) -> float:
    """Weighted total on 0-100, rounded half up to two decimals.

    The weights sum to exactly one, so a candidate scored 100 on every dimension
    totals 100.0 and one scored 0 totals 0.0, with no drift at either end.
    """
    total = sum(
        (Decimal(getattr(scores, dimension)) * weight for dimension, weight in WEIGHTS.items()),
        start=Decimal(0),
    )
    return float(total.quantize(_QUANTUM, rounding=ROUND_HALF_UP))
