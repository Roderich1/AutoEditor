"""NaN and the infinities are refused everywhere a real number is required.

They are not positions in audio, durations, probabilities or ratios; every
ordering comparison against NaN is false, and JSON has no standard spelling for
any of them. Nothing here may coerce one to zero, clamp it to a bound or write
it into an artifact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from content_engine.adapters.media.ffprobe import _parse_frame_rate, _positive_duration
from content_engine.config import Settings, load_settings
from content_engine.domain.exceptions import (
    ConfigurationError,
    InvalidMediaError,
    TranscriptionError,
)
from content_engine.domain.models import (
    MediaInfo,
    NormalizationReport,
    RawTranscription,
    RawWord,
    Transcript,
    TranscriptionMetrics,
    TranscriptSegment,
    TranscriptWord,
)
from content_engine.domain.transcript_rules import normalize_transcription
from content_engine.utils.json import write_json
from tests.conftest import raw_segment, raw_transcription

NON_FINITE = pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="-inf"),
    ],
)


def _media(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "duration_seconds": 10.0,
        "video_codec": "h264",
        "width": 1280,
        "height": 720,
        "fps": 30.0,
        "audio_codec": "aac",
        "sample_rate": 44100,
        "channels": 2,
        "container": "mp4",
        "file_size": 1,
    }
    payload.update(overrides)
    return payload


@NON_FINITE
@pytest.mark.parametrize("field", ["duration_seconds", "fps"])
def test_media_info_refuses_non_finite_numbers(value: float, field: str) -> None:
    with pytest.raises(ValidationError):
        MediaInfo(**_media(**{field: value}))


@NON_FINITE
@pytest.mark.parametrize("field", ["start", "end", "probability"])
def test_a_transcript_word_refuses_non_finite_numbers(value: float, field: str) -> None:
    payload: dict[str, Any] = {"word": "hola", "start": 0.0, "end": 1.0, "probability": 0.9}
    payload[field] = value

    with pytest.raises(ValidationError):
        TranscriptWord(**payload)


@NON_FINITE
@pytest.mark.parametrize("field", ["start", "end"])
def test_a_transcript_segment_refuses_non_finite_numbers(value: float, field: str) -> None:
    payload: dict[str, Any] = {"index": 0, "start": 0.0, "end": 1.0, "text": "hola", "words": []}
    payload[field] = value

    with pytest.raises(ValidationError):
        TranscriptSegment(**payload)


@NON_FINITE
@pytest.mark.parametrize("field", ["duration_seconds", "declared_duration_seconds"])
def test_a_transcript_refuses_a_non_finite_duration(value: float, field: str) -> None:
    payload: dict[str, Any] = {
        "language": "es",
        "language_probability": 0.9,
        "duration_seconds": 10.0,
        "declared_duration_seconds": 10.0,
        "segments": [],
        "model": "tiny",
        "created_at": datetime.now(UTC),
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        Transcript(**payload)


@NON_FINITE
def test_a_transcript_refuses_a_non_finite_language_probability(value: float) -> None:
    with pytest.raises(ValidationError):
        Transcript(
            language="es",
            language_probability=value,
            duration_seconds=10.0,
            declared_duration_seconds=None,
            segments=[],
            model="tiny",
            created_at=datetime.now(UTC),
        )


def _metrics(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "audio_duration_seconds": 10.0,
        "declared_duration_seconds": 10.0,
        "processing_seconds": 1.0,
        "real_time_factor": 0.1,
        "segment_count": 1,
        "word_count": 1,
        "language": "es",
        "language_probability": 0.9,
        "model": "tiny",
        "device_requested": "auto",
        "device_resolved": "cpu",
        "compute_type_requested": "auto",
        "compute_type_resolved": "int8",
        "normalization": NormalizationReport(rules_version=1, tolerance_seconds=0.05),
    }
    payload.update(overrides)
    return payload


@NON_FINITE
@pytest.mark.parametrize(
    "field",
    [
        "audio_duration_seconds",
        "declared_duration_seconds",
        "processing_seconds",
        "real_time_factor",
        "language_probability",
    ],
)
def test_metrics_refuse_non_finite_numbers(value: float, field: str) -> None:
    with pytest.raises(ValidationError):
        TranscriptionMetrics(**_metrics(**{field: value}))


@NON_FINITE
def test_the_normalization_tolerance_must_be_a_real_number(value: float) -> None:
    with pytest.raises(ValidationError):
        NormalizationReport(rules_version=1, tolerance_seconds=value)


@NON_FINITE
def test_a_non_finite_audio_duration_is_refused(value: float) -> None:
    with pytest.raises(TranscriptionError, match="not a finite number"):
        normalize_transcription(
            raw_transcription((raw_segment(0.0, 1.0, "hola"),), None),
            value,
            datetime.now(UTC),
        )


@NON_FINITE
@pytest.mark.parametrize("bound", ["start", "end"])
def test_a_non_finite_segment_bound_is_refused_not_clamped(value: float, bound: str) -> None:
    """NaN compares false against every bound, so it must never reach _pull_up."""
    bounds = {"start": 0.0, "end": 1.0}
    bounds[bound] = value

    with pytest.raises(TranscriptionError, match="not a finite number"):
        normalize_transcription(
            raw_transcription((raw_segment(bounds["start"], bounds["end"], "hola"),), 10.0),
            10.0,
            datetime.now(UTC),
        )


@NON_FINITE
@pytest.mark.parametrize("field", ["start", "end", "probability"])
def test_a_non_finite_word_value_is_refused(value: float, field: str) -> None:
    fields: dict[str, Any] = {"word": "hola", "start": 0.1, "end": 0.5, "probability": 0.9}
    fields[field] = value

    with pytest.raises(TranscriptionError, match="not a finite number"):
        normalize_transcription(
            raw_transcription((raw_segment(0.0, 1.0, "hola", (RawWord(**fields),)),), 10.0),
            10.0,
            datetime.now(UTC),
        )


@NON_FINITE
def test_a_non_finite_declared_duration_is_refused(value: float) -> None:
    with pytest.raises(TranscriptionError, match="not a finite number"):
        normalize_transcription(
            raw_transcription((raw_segment(0.0, 1.0, "hola"),), value),
            10.0,
            datetime.now(UTC),
        )


@NON_FINITE
def test_a_non_finite_language_probability_is_refused(value: float) -> None:
    raw = RawTranscription(
        language="es",
        language_probability=value,
        declared_duration_seconds=10.0,
        segments=(raw_segment(0.0, 1.0, "hola"),),
        model="tiny",
    )

    with pytest.raises(TranscriptionError, match="not a finite number"):
        normalize_transcription(raw, 10.0, datetime.now(UTC))


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "Infinity", "-Infinity", "NaN"])
def test_ffprobe_refuses_a_non_finite_declared_duration(text: str) -> None:
    with pytest.raises(InvalidMediaError):
        _positive_duration(Path("sample.mp4"), text)


@pytest.mark.parametrize("text", ["nan/1", "inf/1", "1/nan"])
def test_an_unusable_frame_rate_degrades_to_zero_rather_than_nan(text: str) -> None:
    assert _parse_frame_rate({"avg_frame_rate": text, "r_frame_rate": text}) == 0.0


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf"])
def test_configuration_refuses_a_non_finite_number(tmp_path: Path, literal: str) -> None:
    profile = tmp_path.joinpath("broken.toml")
    profile.write_text(
        f"[analysis.candidates]\nboundary_snap_seconds = {literal}\n", encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="boundary_snap_seconds"):
        load_settings(profile)


@NON_FINITE
def test_write_json_refuses_to_emit_a_non_standard_literal(tmp_path: Path, value: float) -> None:
    """NaN, Infinity and -Infinity are not JSON; no conforming parser reads them."""
    target = tmp_path.joinpath("artifact.json")

    with pytest.raises(ValueError):
        write_json(target, {"value": value})

    assert not target.exists()
    assert not list(tmp_path.glob("**/*.tmp"))


def test_the_effective_configuration_carries_no_non_standard_literal(
    settings: Settings, tmp_path: Path
) -> None:
    """json.loads would happily accept NaN back; parse_constant proves none is there."""
    target = tmp_path.joinpath("config.effective.json")
    write_json(target, settings.model_dump(mode="json"))

    def refuse(name: str) -> object:
        pytest.fail(f"artifact contains the non-standard literal {name}")

    json.loads(target.read_text(encoding="utf-8"), parse_constant=refuse)
