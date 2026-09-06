"""Domain invariants. The last gate, independent of the normalizer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from content_engine.domain.models import (
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _word(word: str = "x", start: float = 0.0, end: float = 1.0) -> TranscriptWord:
    return TranscriptWord(word=word, start=start, end=end)


def _segment(
    index: int = 0,
    start: float = 0.0,
    end: float = 1.0,
    words: list[TranscriptWord] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(index=index, start=start, end=end, text="t", words=words or [])


def _transcript(segments: list[TranscriptSegment], duration: float = 10.0) -> Transcript:
    return Transcript(
        language="es",
        language_probability=0.9,
        duration_seconds=duration,
        declared_duration_seconds=duration,
        segments=segments,
        model="m",
        created_at=CREATED_AT,
    )


def test_a_word_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="precedes start"):
        _word(start=2.0, end=1.0)


def test_a_word_cannot_start_before_zero() -> None:
    with pytest.raises(ValidationError):
        _word(start=-1.0, end=1.0)


def test_a_segment_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="precedes start"):
        _segment(start=5.0, end=1.0)


def test_a_segment_refuses_a_word_outside_its_bounds() -> None:
    words = [_word(start=3.0, end=4.0)]

    with pytest.raises(ValidationError, match="falls outside"):
        _segment(start=1.0, end=2.0, words=words)


def test_a_segment_refuses_words_in_the_wrong_order() -> None:
    words = [_word("dos", 1.5, 1.8), _word("uno", 1.0, 1.2)]
    with pytest.raises(ValidationError, match="out of order"):
        _segment(start=1.0, end=2.0, words=words)


def test_a_transcript_refuses_non_contiguous_indices() -> None:
    segments = [_segment(index=0), _segment(index=5, start=2.0, end=3.0)]
    with pytest.raises(ValidationError, match="not contiguous"):
        _transcript(segments)


def test_a_transcript_refuses_segments_out_of_order() -> None:
    segments = [_segment(index=0, start=5.0, end=6.0), _segment(index=1, start=1.0, end=2.0)]
    with pytest.raises(ValidationError, match="before its predecessor"):
        _transcript(segments)


def test_a_transcript_refuses_timestamps_past_the_audio_duration() -> None:
    segments = [_segment(end=50.0)]

    with pytest.raises(ValidationError, match="beyond the audio duration"):
        _transcript(segments, duration=10.0)


def test_a_transcript_requires_a_positive_duration() -> None:
    with pytest.raises(ValidationError):
        _transcript([], duration=0.0)


def test_word_count_sums_across_segments() -> None:
    segments = [
        _segment(index=0, start=0.0, end=1.0, words=[_word("a", 0.0, 0.5), _word("b", 0.5, 1.0)]),
        _segment(index=1, start=1.0, end=2.0, words=[_word("c", 1.0, 2.0)]),
    ]

    assert _transcript(segments).word_count == 3


def test_a_clean_transcript_is_accepted() -> None:
    segments = [_segment(index=0, start=0.0, end=1.0, words=[_word("a", 0.0, 1.0)])]

    assert _transcript(segments).segments[0].words[0].word == "a"
