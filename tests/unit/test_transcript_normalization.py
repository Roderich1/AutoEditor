from __future__ import annotations

from datetime import UTC, datetime

import pytest

from content_engine.domain.exceptions import TranscriptionError
from content_engine.domain.models import RawSegment, RawWord
from content_engine.domain.transcript_rules import (
    DURATION_TOLERANCE_SECONDS,
    NORMALIZATION_RULES_VERSION,
    TIMESTAMP_TOLERANCE_SECONDS,
    normalize_transcription,
)
from tests.conftest import raw_segment, raw_transcription, raw_word

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
TOLERANCE = TIMESTAMP_TOLERANCE_SECONDS


def _normalize(
    segments: tuple[RawSegment, ...], duration: float = 10.0, declared: float | None = 10.0
):
    return normalize_transcription(raw_transcription(segments, declared), duration, CREATED_AT)


def test_clean_output_passes_through_unchanged() -> None:
    words = (raw_word("Hola", 0.25, 0.8), raw_word("mundo", 0.9, 1.75))
    transcript, report = _normalize((raw_segment(0.25, 1.75, "Hola mundo", words),))

    assert [segment.text for segment in transcript.segments] == ["Hola mundo"]
    assert transcript.word_count == 2
    assert not report.applied
    assert report.rules_version == NORMALIZATION_RULES_VERSION


def test_whitespace_is_collapsed_without_changing_meaning() -> None:
    transcript, report = _normalize((raw_segment(0.0, 1.0, "  Hola   \n mundo  "),))

    assert transcript.segments[0].text == "Hola mundo"
    assert not report.applied


def test_empty_segments_are_dropped_and_counted() -> None:
    transcript, report = _normalize(
        (
            raw_segment(0.0, 1.0, "uno"),
            raw_segment(1.0, 2.0, "   "),
            raw_segment(2.0, 3.0, "tres"),
        )
    )

    assert [segment.text for segment in transcript.segments] == ["uno", "tres"]
    assert [segment.index for segment in transcript.segments] == [0, 1]
    assert report.dropped_empty_segments == 1


def test_a_transcript_with_no_speech_is_accepted() -> None:
    transcript, report = _normalize(())

    assert transcript.segments == []
    assert transcript.word_count == 0
    assert not report.applied


class TestToleratedCorrections:
    """Differences attributable to floating point are corrected and recorded."""

    def test_segment_end_just_past_the_audio_is_clamped(self) -> None:
        transcript, report = _normalize((raw_segment(9.0, 10.0 + TOLERANCE, "final"),))

        assert transcript.segments[0].end == 10.0
        assert report.clamped_segment_bounds == 1
        assert report.notes

    def test_slightly_negative_start_is_raised_to_zero(self) -> None:
        transcript, report = _normalize((raw_segment(-TOLERANCE, 1.0, "inicio"),))

        assert transcript.segments[0].start == 0.0
        assert report.clamped_segment_bounds == 1

    def test_word_marginally_outside_its_segment_is_clamped(self) -> None:
        words = (raw_word("borde", 1.0 - TOLERANCE, 2.0 + TOLERANCE),)
        transcript, report = _normalize((raw_segment(1.0, 2.0, "borde", words),))

        word = transcript.segments[0].words[0]
        assert (word.start, word.end) == (1.0, 2.0)
        assert report.clamped_word_bounds == 2

    def test_segment_end_marginally_before_its_start_is_flattened(self) -> None:
        transcript, report = _normalize((raw_segment(5.0, 5.0 - TOLERANCE, "ruido"),))

        segment = transcript.segments[0]
        assert segment.start == segment.end == 5.0
        assert report.clamped_segment_bounds == 1


class TestRejections:
    """Anything larger than the tolerance is a real disagreement and is refused."""

    def test_materially_out_of_order_segments_are_refused(self) -> None:
        segments = (raw_segment(5.0, 6.0, "segundo"), raw_segment(1.0, 2.0, "primero"))

        with pytest.raises(TranscriptionError, match="is before"):
            _normalize(segments)

    def test_word_clearly_outside_its_segment_is_refused(self) -> None:
        words = (raw_word("fuera", 100.0, 200.0),)

        with pytest.raises(TranscriptionError, match="is beyond"):
            _normalize((raw_segment(1.0, 2.0, "fuera", words),))

    def test_negative_interval_is_refused(self) -> None:
        with pytest.raises(TranscriptionError, match="is before"):
            _normalize((raw_segment(5.0, 1.0, "invertido"),))

    def test_timestamps_beyond_the_real_duration_are_refused(self) -> None:
        with pytest.raises(TranscriptionError, match="is beyond"):
            _normalize((raw_segment(0.0, 45.0, "demasiado largo"),))

    def test_words_out_of_order_inside_a_segment_are_refused(self) -> None:
        words = (raw_word("dos", 1.5, 1.8), raw_word("uno", 1.0, 1.2))

        with pytest.raises(TranscriptionError, match="is before"):
            _normalize((raw_segment(1.0, 2.0, "dos uno", words),))

    def test_a_declared_duration_that_disagrees_with_the_audio_is_refused(self) -> None:
        with pytest.raises(TranscriptionError, match="not trusted"):
            _normalize((raw_segment(0.0, 1.0, "hola"),), duration=10.0, declared=40.0)

    def test_non_positive_audio_duration_is_refused(self) -> None:
        with pytest.raises(TranscriptionError, match="must be positive"):
            _normalize((raw_segment(0.0, 1.0, "hola"),), duration=0.0, declared=0.0)


class TestToleranceBoundaries:
    """The exact limits of what is corrected versus refused."""

    def test_exactly_at_the_timestamp_tolerance_is_corrected(self) -> None:
        _, report = _normalize((raw_segment(0.0, 10.0 + TOLERANCE, "limite"),))
        assert report.clamped_segment_bounds == 1

    def test_just_past_the_timestamp_tolerance_is_refused(self) -> None:
        with pytest.raises(TranscriptionError):
            _normalize((raw_segment(0.0, 10.0 + TOLERANCE * 2, "pasado"),))

    def test_exactly_at_the_duration_tolerance_is_accepted(self) -> None:
        transcript, _ = _normalize(
            (raw_segment(0.0, 1.0, "hola"),),
            duration=10.0,
            declared=10.0 + DURATION_TOLERANCE_SECONDS,
        )
        assert transcript.declared_duration_seconds == 10.0 + DURATION_TOLERANCE_SECONDS

    def test_just_past_the_duration_tolerance_is_refused(self) -> None:
        with pytest.raises(TranscriptionError, match="not trusted"):
            _normalize(
                (raw_segment(0.0, 1.0, "hola"),),
                duration=10.0,
                declared=10.0 + DURATION_TOLERANCE_SECONDS * 2,
            )


def test_declared_duration_is_kept_separate_from_the_measured_one() -> None:
    transcript, _ = _normalize((raw_segment(0.0, 1.0, "hola"),), duration=10.0, declared=9.7)

    assert transcript.duration_seconds == 10.0
    assert transcript.declared_duration_seconds == 9.7


def test_an_absent_declared_duration_is_allowed() -> None:
    transcript, _ = _normalize((raw_segment(0.0, 1.0, "hola"),), declared=None)

    assert transcript.declared_duration_seconds is None
    assert transcript.duration_seconds == 10.0


def test_notes_are_bounded() -> None:
    segments = tuple(
        RawSegment(
            start=float(index),
            end=float(index) + 1.0,
            text=f"segmento {index}",
            words=(RawWord(word="x", start=float(index) - TOLERANCE, end=float(index) + 0.5),),
        )
        for index in range(40)
    )
    _, report = normalize_transcription(
        raw_transcription(segments, declared_duration_seconds=60.0), 60.0, CREATED_AT
    )

    assert len(report.notes) <= 21
    assert report.notes[-1] == "further normalizations omitted"
