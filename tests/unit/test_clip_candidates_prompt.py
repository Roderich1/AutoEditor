"""CE-026: the real prompt has an identity, and says the things it must say.

These assert properties rather than exact wording. A prompt is going to be
rewritten many times while the engine is tuned, and a test that pinned its text
would have to be edited on every rewrite — which is exactly when nobody reads
what they are editing. What must not silently disappear is the safety half:
that the transcript is data, that spoken instructions are never obeyed, that no
total score is requested, and that virality is not promised.

The hash is asserted to be derived rather than asserted to be a constant, for
the same reason. A literal expected digest here would be updated by copying
whatever the code printed, which tests nothing at all.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from content_engine.adapters.analysis.prompt import (
    PROMPT_IDENTITY,
    PROMPT_SHA256,
    PROMPT_TEXT,
    PROMPT_VERSION,
    load_prompt_text,
    prompt_digest,
)

from content_engine.domain.enums import ClipCategory

SCORE_WEIGHTS = {
    "hook": "25",
    "value": "20",
    "context_independence": "20",
    "clarity": "15",
    "engagement_potential": "10",
    "relevance": "10",
}


def test_the_prompt_version_is_the_one_the_roadmap_names() -> None:
    assert PROMPT_VERSION == "clip_candidates/v1"
    assert PROMPT_IDENTITY.version == PROMPT_VERSION


def test_the_prompt_loads_from_the_package_rather_than_the_working_directory() -> None:
    """importlib.resources, so an installed wheel finds it from anywhere."""
    assert load_prompt_text() == PROMPT_TEXT
    assert len(PROMPT_TEXT) > 500


def test_the_digest_is_derived_from_the_text_and_not_written_down_twice() -> None:
    assert hashlib.sha256(PROMPT_TEXT.encode("utf-8")).hexdigest() == PROMPT_SHA256
    assert PROMPT_IDENTITY.sha256 == PROMPT_SHA256
    assert re.fullmatch(r"[0-9a-f]{64}", PROMPT_SHA256)


def test_changing_one_character_changes_the_digest() -> None:
    assert prompt_digest(PROMPT_TEXT + " ") != PROMPT_SHA256


def test_the_digest_ignores_the_line_endings_a_checkout_may_impose() -> None:
    """The same prompt must hash identically on Windows and on CI.

    Without normalisation a CRLF checkout would produce a different prompt hash
    for the same prompt, and every manifest written on one platform would look
    like a different experiment from the same run on the other.
    """
    assert prompt_digest(PROMPT_TEXT.replace("\n", "\r\n")) == PROMPT_SHA256


def test_the_packaged_text_carries_no_carriage_returns() -> None:
    assert "\r" not in PROMPT_TEXT


@pytest.mark.parametrize("category", [member.value for member in ClipCategory])
def test_every_category_the_domain_accepts_is_defined_in_the_prompt(category: str) -> None:
    """A category the model is never told about is one it can never return."""
    assert category in PROMPT_TEXT


@pytest.mark.parametrize(("dimension", "weight"), sorted(SCORE_WEIGHTS.items()))
def test_every_scored_dimension_and_its_weight_appear(dimension: str, weight: str) -> None:
    assert dimension in PROMPT_TEXT
    assert f"{weight}%" in PROMPT_TEXT


def test_the_prompt_never_asks_for_a_total_score() -> None:
    """ADR-008 puts the arithmetic in Python; asking for it invites inflation."""
    lowered = PROMPT_TEXT.lower()
    assert "total_score" in lowered, "the prompt must mention it in order to forbid it"
    forbidding = [line for line in PROMPT_TEXT.splitlines() if "total_score" in line.lower()]
    assert forbidding
    assert all("not" in line.lower() or "never" in line.lower() for line in forbidding), forbidding


@pytest.mark.parametrize(
    "requirement",
    [
        "data, not instructions",
        "never follow",
        "do not execute",
        "do not invent",
        "self-contained",
    ],
)
def test_the_safety_instructions_are_present(requirement: str) -> None:
    assert requirement in PROMPT_TEXT.lower()


def test_the_prompt_refuses_to_promise_virality() -> None:
    lowered = PROMPT_TEXT.lower()
    assert "viral" in lowered
    for line in PROMPT_TEXT.splitlines():
        if "viral" in line.lower():
            assert "not" in line.lower() or "never" in line.lower(), line


def test_the_duration_policy_is_stated_as_a_range() -> None:
    assert "20" in PROMPT_TEXT
    assert "90" in PROMPT_TEXT


def test_zero_candidates_is_offered_as_a_legitimate_answer() -> None:
    """Otherwise a chunk with nothing in it produces invented material."""
    lowered = PROMPT_TEXT.lower()
    assert "zero" in lowered or "empty" in lowered


def test_the_run_objective_is_not_presented_as_a_per_chunk_quota() -> None:
    """ADR-021. The obvious misreading, spelled out in the prompt itself."""
    assert "run_target_candidates" in PROMPT_TEXT
    quota_lines = [line for line in PROMPT_TEXT.splitlines() if "run_target_candidates" in line]
    assert any("not" in line.lower() for line in quota_lines), quota_lines


def test_timestamps_must_be_grounded_in_the_chunk() -> None:
    lowered = PROMPT_TEXT.lower()
    assert "timestamp" in lowered
    assert "chunk" in lowered
