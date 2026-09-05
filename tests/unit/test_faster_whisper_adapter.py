"""Adapter logic that can be exercised without downloading a model."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.domain.exceptions import TranscriptionError
from content_engine.domain.models import ResolvedHardware, TranscriptionOptions


def _options(device: str = "auto", compute_type: str = "auto") -> TranscriptionOptions:
    return TranscriptionOptions(
        model="tiny",
        device=device,
        compute_type=compute_type,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )


class _FakeCTranslate2:
    def __init__(self, devices: int) -> None:
        self.devices = devices

    def get_cuda_device_count(self) -> int:
        return self.devices


def _with_ctranslate2(monkeypatch: pytest.MonkeyPatch, devices: int | None) -> None:
    def fake_import(name: str) -> Any:
        if name == "ctranslate2":
            if devices is None:
                raise ImportError("ctranslate2 is unavailable")
            return _FakeCTranslate2(devices)
        raise AssertionError(f"unexpected import {name}")

    monkeypatch.setattr(
        "content_engine.adapters.transcription.faster_whisper.importlib.import_module",
        fake_import,
    )


def test_explicit_cpu_defaults_to_int8(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = FasterWhisperTranscriber().resolve_hardware(_options(device="cpu"))

    assert resolved == ResolvedHardware(device="cpu", compute_type="int8")


def test_explicit_cuda_defaults_to_float16() -> None:
    resolved = FasterWhisperTranscriber().resolve_hardware(_options(device="cuda"))

    assert resolved == ResolvedHardware(device="cuda", compute_type="float16")


def test_an_explicit_compute_type_is_respected() -> None:
    resolved = FasterWhisperTranscriber().resolve_hardware(
        _options(device="cuda", compute_type="float32")
    )

    assert resolved.compute_type == "float32"


def test_auto_picks_cuda_when_a_usable_device_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_ctranslate2(monkeypatch, devices=1)

    assert FasterWhisperTranscriber().resolve_hardware(_options()) == ResolvedHardware(
        device="cuda", compute_type="float16"
    )


def test_auto_falls_back_to_cpu_without_a_usable_device(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_ctranslate2(monkeypatch, devices=0)

    assert FasterWhisperTranscriber().resolve_hardware(_options()) == ResolvedHardware(
        device="cpu", compute_type="int8"
    )


def test_auto_stays_conservative_when_the_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GPU may exist while this CTranslate2 build cannot use it."""
    _with_ctranslate2(monkeypatch, devices=None)

    assert FasterWhisperTranscriber().resolve_hardware(_options()).device == "cpu"


def test_auto_keeps_an_explicit_compute_type_on_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_ctranslate2(monkeypatch, devices=0)

    resolved = FasterWhisperTranscriber().resolve_hardware(_options(compute_type="float32"))

    assert resolved == ResolvedHardware(device="cpu", compute_type="float32")


def test_a_missing_package_is_reported_as_a_transcription_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    def fake_import(name: str) -> Any:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(
        "content_engine.adapters.transcription.faster_whisper.importlib.import_module",
        fake_import,
    )

    with pytest.raises(TranscriptionError, match="uv sync --extra transcription"):
        FasterWhisperTranscriber().transcribe(
            tmp_path.joinpath("source.wav"),
            _options(),
            ResolvedHardware(device="cpu", compute_type="int8"),
        )
