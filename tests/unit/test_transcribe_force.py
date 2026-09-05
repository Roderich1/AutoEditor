"""The transcription fingerprint and the reuse decision it drives."""

from __future__ import annotations

import pytest

from content_engine.domain.models import ResolvedHardware, TranscriptionOptions
from content_engine.domain.transcript_rules import transcription_fingerprint

OPTIONS = TranscriptionOptions(
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


def _fingerprint(**overrides: object) -> str:
    audio = str(overrides.pop("audio_sha256", AUDIO))
    hardware = overrides.pop("hardware", CPU)
    assert isinstance(hardware, ResolvedHardware)
    options = (
        OPTIONS if not overrides else TranscriptionOptions(**{**vars_of(OPTIONS), **overrides})
    )
    return transcription_fingerprint(audio, options, hardware)


def vars_of(options: TranscriptionOptions) -> dict[str, object]:
    return {
        "model": options.model,
        "device": options.device,
        "compute_type": options.compute_type,
        "beam_size": options.beam_size,
        "word_timestamps": options.word_timestamps,
        "vad_filter": options.vad_filter,
    }


def test_identical_inputs_produce_the_same_fingerprint() -> None:
    assert _fingerprint() == _fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"audio_sha256": "b" * 64},
        {"model": "small"},
        {"beam_size": 1},
        {"word_timestamps": False},
        {"vad_filter": False},
        {"device": "cpu"},
        {"compute_type": "int8"},
    ],
)
def test_every_declared_input_changes_the_fingerprint(override: dict[str, object]) -> None:
    assert _fingerprint(**override) != _fingerprint()


def test_resolved_hardware_changes_the_fingerprint() -> None:
    """auto on a GPU machine is not the same execution as auto on a CPU machine."""
    assert _fingerprint(hardware=CUDA) != _fingerprint(hardware=CPU)


def test_the_same_request_on_different_hardware_is_not_interchangeable() -> None:
    cpu_run = transcription_fingerprint(AUDIO, OPTIONS, CPU)
    cuda_run = transcription_fingerprint(AUDIO, OPTIONS, CUDA)

    assert cpu_run != cuda_run


def test_fingerprint_is_a_hex_digest() -> None:
    fingerprint = _fingerprint()

    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")
