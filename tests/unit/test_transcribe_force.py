"""The transcription fingerprint and the reuse decision it drives."""

from __future__ import annotations

from typing import Any

import pytest

from content_engine.domain.models import ResolvedHardware, TranscriptionOptions
from content_engine.domain.transcript_rules import (
    NORMALIZATION_RULES_VERSION,
    stage_config,
    stage_config_sha256,
    transcription_fingerprint,
)

OPTIONS = TranscriptionOptions(
    provider="faster-whisper",
    model="large-v3",
    device="auto",
    compute_type="auto",
    beam_size=5,
    word_timestamps=True,
    vad_filter=True,
)
CPU = ResolvedHardware(device="cpu", compute_type="int8")
CUDA = ResolvedHardware(device="cuda", compute_type="float16")
AUDIO = "a" * 64


def _options(**overrides: Any) -> TranscriptionOptions:
    fields: dict[str, Any] = {
        "provider": OPTIONS.provider,
        "model": OPTIONS.model,
        "device": OPTIONS.device,
        "compute_type": OPTIONS.compute_type,
        "beam_size": OPTIONS.beam_size,
        "word_timestamps": OPTIONS.word_timestamps,
        "vad_filter": OPTIONS.vad_filter,
    }
    fields.update(overrides)
    return TranscriptionOptions(**fields)


def _fingerprint(
    audio_sha256: str = AUDIO,
    hardware: ResolvedHardware = CPU,
    **overrides: Any,
) -> str:
    return transcription_fingerprint(audio_sha256, stage_config(_options(**overrides), hardware))


def test_separately_built_but_equal_inputs_produce_the_same_fingerprint() -> None:
    """Determinism means two independent constructions agree, not that a call
    equals itself: the second must be built from scratch to prove anything."""
    first = transcription_fingerprint("b" * 64, stage_config(_options(), CPU))
    second = transcription_fingerprint(
        "b" * 64,
        stage_config(
            TranscriptionOptions(
                provider="faster-whisper",
                model="large-v3",
                device="auto",
                compute_type="auto",
                beam_size=5,
                word_timestamps=True,
                vad_filter=True,
            ),
            ResolvedHardware(device="cpu", compute_type="int8"),
        ),
    )

    assert first == second


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "another-provider"},
        {"model": "small"},
        {"beam_size": 1},
        {"word_timestamps": False},
        {"vad_filter": False},
        {"device": "cpu"},
        {"compute_type": "int8"},
    ],
)
def test_every_declared_input_changes_the_fingerprint(override: dict[str, Any]) -> None:
    assert _fingerprint(**override) != _fingerprint()


def test_different_audio_changes_the_fingerprint() -> None:
    assert _fingerprint(audio_sha256="b" * 64) != _fingerprint()


def test_resolved_hardware_changes_the_fingerprint() -> None:
    """auto on a GPU machine is not the same execution as auto on a CPU machine."""
    assert _fingerprint(hardware=CUDA) != _fingerprint(hardware=CPU)


def test_the_same_request_on_different_hardware_is_not_interchangeable() -> None:
    cpu_run = transcription_fingerprint(AUDIO, stage_config(OPTIONS, CPU))
    cuda_run = transcription_fingerprint(AUDIO, stage_config(OPTIONS, CUDA))

    assert cpu_run != cuda_run


def test_fingerprint_is_a_hex_digest() -> None:
    fingerprint = _fingerprint()

    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")


def test_the_stage_config_records_what_was_requested_and_what_resolved() -> None:
    config = stage_config(_options(), CUDA)

    assert config.device_requested == "auto"
    assert config.device_resolved == "cuda"
    assert config.compute_type_requested == "auto"
    assert config.compute_type_resolved == "float16"
    assert config.normalization_version == NORMALIZATION_RULES_VERSION


def test_the_fingerprint_is_derived_from_the_stage_config_alone() -> None:
    """One canonical payload: anything the artifact records, the digest covers."""
    config = stage_config(_options(), CPU)
    changed = config.model_copy(update={"beam_size": 1})

    assert transcription_fingerprint(AUDIO, changed) != transcription_fingerprint(AUDIO, config)
    assert stage_config_sha256(changed) != stage_config_sha256(config)
