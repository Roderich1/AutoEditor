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
        provider="faster-whisper",
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


class _Word:
    def __init__(self, word: str, start: float | None, end: float | None) -> None:
        self.word = word
        self.start = start
        self.end = end
        self.probability = 0.8


class _Segment:
    def __init__(self, start: float, end: float, text: str, words: list[_Word] | None) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = words


def test_provider_segments_are_translated_verbatim() -> None:
    """Translation only. Correcting timestamps is the domain's job, not the adapter's."""
    segment = _Segment(1.0, 2.0, "  hola  mundo ", [_Word("hola", 1.0, 1.5)])

    converted = FasterWhisperTranscriber._convert_segment(segment)

    assert converted.start == 1.0
    assert converted.end == 2.0
    assert converted.text == "  hola  mundo "
    assert converted.words[0].word == "hola"
    assert converted.words[0].probability == 0.8


def test_words_without_timestamps_are_dropped() -> None:
    words = [_Word("bueno", 1.0, 1.5), _Word("sin-tiempo", None, None)]
    segment = _Segment(1.0, 2.0, "bueno", words)

    converted = FasterWhisperTranscriber._convert_segment(segment)

    assert [word.word for word in converted.words] == ["bueno"]


def test_a_segment_without_words_is_allowed() -> None:
    converted = FasterWhisperTranscriber._convert_segment(_Segment(0.0, 1.0, "texto", None))

    assert converted.words == ()
