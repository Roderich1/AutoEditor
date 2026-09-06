"""CE-022. Metrics are produced without ever loading a real model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_engine.config import Settings
from content_engine.domain.models import ResolvedHardware, TranscriptionMetrics
from content_engine.services.transcription_service import (
    TranscriptionService,
    options_from_settings,
)
from tests.conftest import FakeClock, FakeTranscriber, raw_segment, raw_transcription, raw_word

CUDA = ResolvedHardware(device="cuda", compute_type="float16")

REQUIRED_FIELDS = (
    "audio_duration_seconds",
    "processing_seconds",
    "real_time_factor",
    "segment_count",
    "word_count",
    "language",
    "language_probability",
    "model",
    "device_requested",
    "device_resolved",
    "compute_type_requested",
    "compute_type_resolved",
)


def _transcribe(
    tmp_path: Path,
    settings: Settings,
    segments: tuple,
    duration: float = 10.0,
    step: float = 2.5,
    hardware: ResolvedHardware = CUDA,
) -> tuple[TranscriptionMetrics, Path]:
    audio = tmp_path.joinpath("source.wav")
    audio.write_bytes(b"wav")
    output = tmp_path.joinpath("transcript")
    service = TranscriptionService(
        FakeTranscriber(raw_transcription(segments, duration), hardware=hardware),
        clock=FakeClock(step),
    )
    outcome = service.transcribe(
        audio,
        duration,
        output,
        options_from_settings(settings.transcription),
        hardware,
    )
    return outcome.metrics, output


def test_every_required_metric_is_present(tmp_path: Path, settings: Settings) -> None:
    metrics, output = _transcribe(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))
    payload = json.loads(output.joinpath("metrics.json").read_text(encoding="utf-8"))

    for field in REQUIRED_FIELDS:
        assert field in payload, field
    assert metrics.model == settings.transcription.model


def test_real_time_factor_relates_processing_to_audio(tmp_path: Path, settings: Settings) -> None:
    metrics, _ = _transcribe(
        tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),), duration=10.0, step=2.5
    )

    assert metrics.processing_seconds == pytest.approx(2.5)
    assert metrics.real_time_factor == pytest.approx(0.25)


def test_counts_reflect_the_normalized_transcript(tmp_path: Path, settings: Settings) -> None:
    segments = (
        raw_segment(0.0, 1.0, "uno", (raw_word("uno", 0.0, 1.0),)),
        raw_segment(1.0, 2.0, "  ", (raw_word("ignored", 1.0, 2.0),)),
        raw_segment(2.0, 3.0, "dos tres", (raw_word("dos", 2.0, 2.5), raw_word("tres", 2.5, 3.0))),
    )
    metrics, _ = _transcribe(tmp_path, settings, segments)

    assert metrics.segment_count == 2
    assert metrics.word_count == 3
    assert metrics.normalization.dropped_empty_segments == 1


def test_resolved_hardware_is_recorded_next_to_what_was_requested(
    tmp_path: Path, settings: Settings
) -> None:
    """device = auto in the profile, CUDA in reality: both are recorded."""
    metrics, _ = _transcribe(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))

    assert metrics.device_requested == "auto"
    assert metrics.compute_type_requested == "auto"
    assert metrics.device_resolved == "cuda"
    assert metrics.compute_type_resolved == "float16"


def test_language_detection_is_reported(tmp_path: Path, settings: Settings) -> None:
    metrics, _ = _transcribe(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))

    assert metrics.language == "es"
    assert metrics.language_probability == pytest.approx(0.99)


def test_an_empty_transcript_still_produces_metrics(tmp_path: Path, settings: Settings) -> None:
    metrics, output = _transcribe(tmp_path, settings, ())

    assert output.joinpath("metrics.json").is_file()
    assert metrics.segment_count == 0
    assert metrics.word_count == 0
    assert metrics.real_time_factor is not None


def test_normalization_report_is_part_of_the_metrics(tmp_path: Path, settings: Settings) -> None:
    metrics, output = _transcribe(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))
    payload = json.loads(output.joinpath("metrics.json").read_text(encoding="utf-8"))

    assert payload["normalization"]["rules_version"] == metrics.normalization.rules_version
    assert payload["normalization"]["tolerance_seconds"] == metrics.normalization.tolerance_seconds
