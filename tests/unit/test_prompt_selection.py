"""`analysis.prompt_version` selects a prompt, or the run refuses to start.

The setting existed in `default.toml` from the beginning and, until this change,
did nothing at all: the adapter used the one packaged prompt whatever the
configuration said. A profile asking for `v2` therefore ran `v1` and recorded
`clip_candidates/v1` in its artifacts, so an experiment could believe it had
changed the prompt while changing nothing. Reuse made it worse — the stage
configuration was identical either way, so the second run was a silent reuse of
the first.

The rule now is that the configured version decides the resource, the text, the
identity, the digest, the stage configuration, the fingerprint and therefore
reuse. There is currently exactly one prompt to select, which is precisely when
this is worth pinning down: the machinery has to be right before there are two.
"""

from __future__ import annotations

import pytest

from content_engine.adapters.analysis.prompt import (
    PROMPT_SHA256,
    PROMPT_TEXT,
    PROMPT_VERSION,
    available_prompt_versions,
    select_prompt,
)
from content_engine.domain.exceptions import EXIT_CONFIGURATION, ConfigurationError


def test_the_configured_v1_selects_the_packaged_clip_candidates_prompt() -> None:
    prompt = select_prompt("v1")

    assert prompt.configured == "v1"
    assert prompt.version == PROMPT_VERSION == "clip_candidates/v1"
    assert prompt.sha256 == PROMPT_SHA256
    assert prompt.text == PROMPT_TEXT


def test_the_selected_prompt_carries_its_own_identity() -> None:
    """What the adapter records comes from the selection, not from a constant."""
    prompt = select_prompt("v1")

    assert prompt.identity.version == prompt.version
    assert prompt.identity.sha256 == prompt.sha256


def test_the_configured_name_and_the_recorded_identity_are_different_things() -> None:
    """`v1` is what a profile writes; `clip_candidates/v1` is what a run records.

    Keeping both means a reader of an artifact never has to guess which family
    of prompts a bare `v1` belonged to.
    """
    prompt = select_prompt("v1")

    assert prompt.configured != prompt.version
    assert prompt.version.endswith(f"/{prompt.configured}")


def test_only_the_versions_that_exist_are_offered() -> None:
    assert available_prompt_versions() == ("v1",)


@pytest.mark.parametrize("unknown", ["v2", "v0", "clip_candidates/v1", "", "V1", "latest"])
def test_an_unknown_version_is_a_configuration_error(unknown: str) -> None:
    """Exit 2, and it names what it does have rather than only what it refused."""
    with pytest.raises(ConfigurationError) as caught:
        select_prompt(unknown)

    assert caught.value.exit_code == EXIT_CONFIGURATION
    assert repr(unknown) in str(caught.value)
    assert "v1" in str(caught.value)


def test_the_refusal_mentions_the_setting_that_has_to_change() -> None:
    with pytest.raises(ConfigurationError, match="analysis.prompt_version"):
        select_prompt("v2")
