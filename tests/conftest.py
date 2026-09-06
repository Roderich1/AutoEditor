from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from content_engine.config import WORKSPACE_ENV_VAR, Settings, load_settings
from content_engine.domain.models import (
    RawSegment,
    RawTranscription,
    RawWord,
    ResolvedHardware,
    TranscriptionOptions,
)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Canonical settings with the workspace redirected into the test directory."""
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("workspace")))
    monkeypatch.delenv("CONTENT_ENGINE_ANALYSIS_MODEL", raising=False)
    return load_settings()


def fake_process(arguments: Sequence[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(arguments), 0, stdout, "")


def raw_word(word: str, start: float, end: float) -> RawWord:
    return RawWord(word=word, start=start, end=end, probability=0.9)


def raw_segment(start: float, end: float, text: str, words: tuple[RawWord, ...] = ()) -> RawSegment:
    return RawSegment(start=start, end=end, text=text, words=words)


def raw_transcription(
    segments: tuple[RawSegment, ...],
    declared_duration_seconds: float | None = 10.0,
    language: str = "es",
) -> RawTranscription:
    return RawTranscription(
        language=language,
        language_probability=0.99,
        declared_duration_seconds=declared_duration_seconds,
        segments=segments,
        model="fake-model",
    )


@dataclass
class FakeTranscriber:
    """Deterministic stand-in for faster-whisper. Never downloads a model."""

    result: RawTranscription
    hardware: ResolvedHardware = ResolvedHardware(device="cpu", compute_type="int8")
    calls: list[TranscriptionOptions] = field(default_factory=list)

    def resolve_hardware(self, options: TranscriptionOptions) -> ResolvedHardware:
        return self.hardware

    def transcribe(
        self,
        audio_path: Path,
        options: TranscriptionOptions,
        hardware: ResolvedHardware,
    ) -> RawTranscription:
        self.calls.append(options)
        # The real adapter reports back the model it was asked to load, so the
        # fake must too: a fake that answers with a fixed name would hide any
        # disagreement between the requested model and the recorded one.
        return replace(self.result, model=options.model)


class FakeClock:
    """Monotonic clock that advances a fixed amount per reading pair."""

    def __init__(self, step: float = 2.0) -> None:
        self.step = step
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current
