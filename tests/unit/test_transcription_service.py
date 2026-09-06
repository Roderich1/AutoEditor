from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_engine.config import Settings
from content_engine.domain.models import (
    METRICS_SCHEMA_VERSION,
    TRANSCRIPT_SCHEMA_VERSION,
    ResolvedHardware,
)
from content_engine.services.transcription_service import (
    TranscriptionService,
    options_from_settings,
)
from tests.conftest import FakeClock, FakeTranscriber, raw_segment, raw_transcription, raw_word

HARDWARE = ResolvedHardware(device="cpu", compute_type="int8")


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("source.wav")
    path.write_bytes(b"wav")
    return path


def _run(
    tmp_path: Path,
    settings: Settings,
    segments: tuple,
    declared: float | None = 10.0,
    duration: float = 10.0,
    step: float = 2.0,
):
    audio = tmp_path.joinpath("source.wav")
    audio.write_bytes(b"wav")
    output = tmp_path.joinpath("transcript")
    service = TranscriptionService(
        FakeTranscriber(raw_transcription(segments, declared)),
        clock=FakeClock(step),
    )
    outcome = service.transcribe(
        audio,
        duration,
        output,
        options_from_settings(settings.transcription),
        HARDWARE,
    )
    return outcome, output


def test_exports_all_formats(tmp_path: Path, settings: Settings) -> None:
    words = (raw_word("Hola", 0.25, 0.8), raw_word("mundo", 0.9, 1.75))
    _, output = _run(tmp_path, settings, (raw_segment(0.25, 1.75, "Hola mundo", words),))

    assert output.joinpath("transcript.json").is_file()
    assert output.joinpath("metrics.json").is_file()
    assert output.joinpath("transcript.txt").read_text(encoding="utf-8") == "Hola mundo\n"
    srt = output.joinpath("transcript.srt").read_text(encoding="utf-8")
    assert "00:00:00,250 --> 00:00:01,750" in srt


def test_srt_numbering_stays_contiguous_when_segments_are_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    """An empty segment must not leave a hole in the cue sequence."""
    segments = (
        raw_segment(0.0, 1.0, "uno"),
        raw_segment(1.0, 2.0, "   "),
        raw_segment(2.0, 3.0, "tres"),
    )
    _, output = _run(tmp_path, settings, segments)
    srt = output.joinpath("transcript.srt").read_text(encoding="utf-8")

    assert [line for line in srt.splitlines() if line.isdigit()] == ["1", "2"]
    assert "uno" in srt
    assert "tres" in srt


def test_empty_transcript_produces_empty_files_not_stray_newlines(
    tmp_path: Path, settings: Settings
) -> None:
    outcome, output = _run(tmp_path, settings, ())

    assert output.joinpath("transcript.txt").read_bytes() == b""
    assert output.joinpath("transcript.srt").read_bytes() == b""
    assert outcome.metrics.segment_count == 0


def test_artifacts_use_lf_without_a_bom(tmp_path: Path, settings: Settings) -> None:
    segments = (raw_segment(0.0, 1.0, "uno"), raw_segment(1.0, 2.0, "dos"))
    _, output = _run(tmp_path, settings, segments)

    for name in ("transcript.json", "metrics.json", "transcript.txt", "transcript.srt"):
        data = output.joinpath(name).read_bytes()
        assert b"\r\n" not in data, name
        assert not data.startswith(b"\xef\xbb\xbf"), name


def test_transcript_json_declares_its_schema(tmp_path: Path, settings: Settings) -> None:
    _, output = _run(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))
    payload = json.loads(output.joinpath("transcript.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == TRANSCRIPT_SCHEMA_VERSION
    assert payload["duration_seconds"] == 10.0
    assert payload["declared_duration_seconds"] == 10.0


def test_hardware_resolution_is_delegated_to_the_adapter(settings: Settings) -> None:
    transcriber = FakeTranscriber(
        raw_transcription(()),
        hardware=ResolvedHardware(device="cuda", compute_type="float16"),
    )
    service = TranscriptionService(transcriber)

    resolved = service.resolve_hardware(options_from_settings(settings.transcription))

    assert (resolved.device, resolved.compute_type) == ("cuda", "float16")


def test_options_are_taken_from_settings(tmp_path: Path, settings: Settings) -> None:
    transcriber = FakeTranscriber(raw_transcription((raw_segment(0.0, 1.0, "uno"),)))
    service = TranscriptionService(transcriber, clock=FakeClock())
    audio = tmp_path.joinpath("source.wav")
    audio.write_bytes(b"wav")

    service.transcribe(
        audio,
        10.0,
        tmp_path.joinpath("transcript"),
        options_from_settings(settings.transcription),
        HARDWARE,
    )

    used = transcriber.calls[0]
    assert used.model == settings.transcription.model
    assert used.beam_size == settings.transcription.beam_size
    assert used.word_timestamps is settings.transcription.word_timestamps
    assert used.vad_filter is settings.transcription.vad_filter


def test_metrics_schema_is_declared(tmp_path: Path, settings: Settings) -> None:
    outcome, output = _run(tmp_path, settings, (raw_segment(0.0, 1.0, "uno"),))
    payload = json.loads(output.joinpath("metrics.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == METRICS_SCHEMA_VERSION
    assert outcome.metrics.schema_version == METRICS_SCHEMA_VERSION
