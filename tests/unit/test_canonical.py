"""The one canonical serialization, and proof that extracting it changed nothing.

The digests below were computed on `main` at d047479, before `_canonical` moved
out of `transcript_rules` into `utils.canonical`. They are pinned here as
literals rather than recomputed, because a test that recomputes them from the
code under test would agree with any refactor, including one that silently
invalidated every run already on disk.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from content_engine.domain.models import ResolvedHardware, TranscriptionOptions
from content_engine.domain.transcript_rules import (
    stage_config,
    stage_config_sha256,
    transcription_fingerprint,
)
from content_engine.utils.canonical import canonical_json, canonical_sha256

#: Frozen on main at d047479. Changing either value breaks reuse for every run
#: already produced, so a change here has to be a deliberate schema bump.
PINNED_OPTIONS = TranscriptionOptions(
    provider="faster-whisper",
    model="large-v3",
    device="auto",
    compute_type="auto",
    beam_size=5,
    word_timestamps=True,
    vad_filter=True,
)
PINNED_HARDWARE = ResolvedHardware(device="cpu", compute_type="int8")
PINNED_AUDIO_SHA256 = "a" * 64
PINNED_STAGE_CONFIG_SHA256 = "916c3dcc3d6fb06b92df68d2903ad34137eeee31aff94410476d1de8cb9062bc"
PINNED_FINGERPRINT = "ecfbcdca500172ec43c6283710a8f689da5f51838fa649421dada8665a70b176"


def test_the_stage_configuration_digest_is_unchanged_by_the_extraction() -> None:
    config = stage_config(PINNED_OPTIONS, PINNED_HARDWARE)

    assert stage_config_sha256(config) == PINNED_STAGE_CONFIG_SHA256


def test_the_transcription_fingerprint_is_unchanged_by_the_extraction() -> None:
    config = stage_config(PINNED_OPTIONS, PINNED_HARDWARE)

    assert transcription_fingerprint(PINNED_AUDIO_SHA256, config) == PINNED_FINGERPRINT


def test_key_order_does_not_change_the_serialization() -> None:
    forward = {"alpha": 1, "beta": 2, "gamma": 3}
    reversed_order = {"gamma": 3, "beta": 2, "alpha": 1}

    assert canonical_json(forward) == canonical_json(reversed_order)
    assert canonical_sha256(forward) == canonical_sha256(reversed_order)


def test_the_serialization_carries_no_incidental_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_non_ascii_is_written_as_itself_rather_than_escaped() -> None:
    """A Spanish topic hashes as its own characters, not as \\u sequences."""
    serialized = canonical_json({"topic": "configuración de red"})

    assert "configuración" in serialized
    assert "\\u" not in serialized


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="-inf"),
    ],
)
def test_a_non_finite_value_can_never_reach_a_digest(value: float) -> None:
    payload: dict[str, Any] = {"value": value}

    with pytest.raises(ValueError):
        canonical_json(payload)


def test_the_digest_is_a_lowercase_hex_sha256() -> None:
    digest = canonical_sha256({"a": 1})

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_serialization_round_trips_through_a_standard_parser() -> None:
    payload = {"b": [1, {"c": "ñ"}], "a": None, "d": True}

    assert json.loads(canonical_json(payload)) == payload
