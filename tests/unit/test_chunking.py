"""Transcript chunking (CE-023).

The properties under test are the ones a wrong chunker would break silently:
segments stay whole, timestamps stay absolute, the overlap really overlaps, and
an empty transcript is an outcome rather than an exception.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from content_engine.config import ChunkingSettings
from content_engine.domain.candidates import CHUNKS_SCHEMA_VERSION, TranscriptChunk
from content_engine.domain.models import Transcript, TranscriptSegment, TranscriptWord
from content_engine.services.chunking_service import (
    CHUNKING_RULES_VERSION,
    build_chunk_collection,
    build_chunks,
    transcript_sha256,
)

BASELINE = ChunkingSettings(window_seconds=360, overlap_seconds=30)
STRIDE = BASELINE.window_seconds - BASELINE.overlap_seconds  # 330


def _segment(index: int, start: float, end: float, text: str = "hola") -> TranscriptSegment:
    return TranscriptSegment(index=index, start=start, end=end, text=text, words=[])


def _transcript(segments: list[TranscriptSegment], duration: float | None = None) -> Transcript:
    measured = duration if duration is not None else (segments[-1].end if segments else 1.0)
    return Transcript(
        language="es",
        language_probability=0.99,
        duration_seconds=measured,
        declared_duration_seconds=measured,
        segments=segments,
        model="tiny",
        created_at=datetime.now(UTC),
    )


def _evenly_spaced(count: int, length: float = 10.0) -> list[TranscriptSegment]:
    return [_segment(i, i * length, (i + 1) * length, f"segmento {i}") for i in range(count)]


def test_an_empty_transcript_produces_no_chunks() -> None:
    """No recognisable speech is a real outcome, not a failure."""
    assert build_chunks(_transcript([], duration=120.0), BASELINE) == []


def test_a_single_short_segment_produces_one_chunk() -> None:
    chunks = build_chunks(_transcript([_segment(0, 0.0, 5.0)]), BASELINE)

    assert len(chunks) == 1
    assert chunks[0].segment_indices == [0]
    assert chunks[0].start == 0.0
    assert chunks[0].end == 5.0


def test_a_transcript_shorter_than_one_window_is_one_chunk() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(30)), BASELINE)  # 300 s

    assert len(chunks) == 1
    assert chunks[0].window_start == 0.0
    assert chunks[0].window_end == 360.0


def test_a_transcript_of_exactly_one_window_is_one_chunk() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(36)), BASELINE)  # exactly 360 s

    assert len(chunks) == 1


def test_a_transcript_just_past_one_window_produces_a_second_chunk() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(37)), BASELINE)  # 370 s

    assert len(chunks) == 2
    assert chunks[1].window_start == float(STRIDE)


def test_windows_advance_by_the_stride_not_the_window() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(120)), BASELINE)  # 1200 s

    starts = [chunk.window_start for chunk in chunks]
    assert starts == [float(STRIDE * i) for i in range(len(chunks))]


def test_consecutive_chunks_really_share_the_overlap() -> None:
    """The overlap is what lets an idea crossing a boundary be seen whole."""
    chunks = build_chunks(_transcript(_evenly_spaced(80)), BASELINE)  # 800 s

    shared = set(chunks[0].segment_indices) & set(chunks[1].segment_indices)

    assert shared, "consecutive chunks must share the overlap region"
    for position in shared:
        segment = chunks[0].segments[chunks[0].segment_indices.index(position)]
        assert segment.end > chunks[1].window_start


def test_a_segment_crossing_a_boundary_is_never_split() -> None:
    """A window is arithmetic; a segment is meaning. The segment wins."""
    straddling = _segment(0, 355.0, 365.0, "cruza la frontera")
    segments = [*_evenly_spaced(35), straddling.model_copy(update={"index": 35})]
    segments.append(_segment(36, 366.0, 700.0, "despues"))

    chunks = build_chunks(_transcript(segments), BASELINE)

    for chunk in chunks:
        for segment in chunk.segments:
            assert segment.start == segments[segment.index].start
            assert segment.end == segments[segment.index].end
    holders = [chunk.index for chunk in chunks if 35 in chunk.segment_indices]
    assert len(holders) >= 1


def test_the_real_extent_may_reach_past_the_nominal_window() -> None:
    segments = [_segment(0, 0.0, 5.0), _segment(1, 355.0, 372.0, "cruza")]

    chunk = build_chunks(_transcript(segments), BASELINE)[0]

    assert chunk.window_end == 360.0
    assert chunk.end == 372.0, "the chunk reports what the analyzer actually saw"


def test_timestamps_are_absolute_and_never_rebased() -> None:
    segments = _evenly_spaced(80)

    chunks = build_chunks(_transcript(segments), BASELINE)

    for chunk in chunks:
        for position, segment in zip(chunk.segment_indices, chunk.segments, strict=True):
            assert segment.start == segments[position].start
            assert segment.end == segments[position].end
    assert chunks[1].segments[0].start >= float(STRIDE - 10)


def test_words_survive_into_the_chunk() -> None:
    words = [TranscriptWord(word="hola", start=0.1, end=0.5, probability=0.9)]
    segment = TranscriptSegment(index=0, start=0.0, end=1.0, text="hola", words=words)

    chunk = build_chunks(_transcript([segment]), BASELINE)[0]

    assert chunk.segments[0].words[0].start == 0.1


def test_a_trailing_window_that_adds_nothing_is_not_emitted() -> None:
    """A final window fully covered by the previous one would be a repeat call."""
    segments = [_segment(0, 0.0, 5.0), _segment(1, 340.0, 345.0)]

    chunks = build_chunks(_transcript(segments, duration=900.0), BASELINE)

    assert len(chunks) == 1
    assert chunks[0].segment_indices == [0, 1]


def test_a_genuine_partial_last_chunk_is_emitted() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(75)), BASELINE)  # 750 s

    assert len(chunks) >= 2
    assert chunks[-1].segment_indices[-1] == 74
    assert chunks[-1].end == 750.0


def test_chunk_ids_are_contiguous_and_zero_padded() -> None:
    chunks = build_chunks(_transcript(_evenly_spaced(120)), BASELINE)

    assert [chunk.id for chunk in chunks] == [f"chunk_{i:04d}" for i in range(len(chunks))]
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_every_segment_appears_in_at_least_one_chunk() -> None:
    segments = _evenly_spaced(200)

    chunks = build_chunks(_transcript(segments), BASELINE)

    covered = {index for chunk in chunks for index in chunk.segment_indices}
    assert covered == set(range(len(segments)))


def test_the_rendered_text_carries_absolute_timestamps() -> None:
    chunk = build_chunks(_transcript([_segment(0, 12.34, 56.78, "prueba")]), BASELINE)[0]

    assert "12.34" in chunk.text
    assert "56.78" in chunk.text
    assert "prueba" in chunk.text


def test_the_rendered_text_is_byte_stable() -> None:
    """The render is part of what the model is asked; it must not drift."""
    chunk = build_chunks(_transcript([_segment(0, 1.5, 2.25, "hola mundo")]), BASELINE)[0]

    assert chunk.text == "[     1.50 -->      2.25] hola mundo"


def test_chunking_is_deterministic() -> None:
    transcript = _transcript(_evenly_spaced(120))

    first = build_chunks(transcript, BASELINE)
    second = build_chunks(transcript, BASELINE)

    assert [chunk.model_dump(mode="json") for chunk in first] == [
        chunk.model_dump(mode="json") for chunk in second
    ]


@pytest.mark.parametrize(
    ("window", "overlap"),
    [(360, 30), (240, 20), (600, 45), (60, 0)],
)
def test_other_window_settings_still_cover_the_transcript(window: int, overlap: int) -> None:
    settings = ChunkingSettings(window_seconds=window, overlap_seconds=overlap)
    segments = _evenly_spaced(150)

    chunks = build_chunks(_transcript(segments), settings)

    covered = {index for chunk in chunks for index in chunk.segment_indices}
    assert covered == set(range(len(segments)))


def test_the_collection_records_where_the_chunks_came_from() -> None:
    transcript = _transcript(_evenly_spaced(80))

    collection = build_chunk_collection(transcript, BASELINE)

    assert collection.schema_version == CHUNKS_SCHEMA_VERSION
    assert collection.rules_version == CHUNKING_RULES_VERSION
    assert collection.window_seconds == 360
    assert collection.overlap_seconds == 30
    assert collection.transcript_sha256 == transcript_sha256(transcript)
    assert collection.source_duration_seconds == transcript.duration_seconds
    assert len(collection.chunks) == len(build_chunks(transcript, BASELINE))


def test_the_transcript_digest_changes_with_the_transcript() -> None:
    first = _transcript(_evenly_spaced(10))
    second = _transcript(_evenly_spaced(11))

    assert transcript_sha256(first) != transcript_sha256(second)


def test_the_collection_round_trips_through_json() -> None:
    collection = build_chunk_collection(_transcript(_evenly_spaced(80)), BASELINE)

    restored = type(collection).model_validate(collection.model_dump(mode="json"))

    assert restored == collection


def test_a_chunk_cannot_claim_an_extent_its_segments_do_not_support() -> None:
    with pytest.raises(ValueError, match="earliest segment start"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=0.0,
            window_end=360.0,
            start=5.0,
            end=10.0,
            segment_indices=[0],
            segments=[_segment(0, 0.0, 10.0)],
            text="x",
        )


def test_a_chunk_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="no segments"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=0.0,
            window_end=360.0,
            start=0.0,
            end=1.0,
            segment_indices=[],
            segments=[],
            text="",
        )


def test_a_chunk_cannot_mismatch_its_indices_and_segments() -> None:
    with pytest.raises(ValueError, match="lists 2 indices"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=0.0,
            window_end=360.0,
            start=0.0,
            end=10.0,
            segment_indices=[0, 1],
            segments=[_segment(0, 0.0, 10.0)],
            text="x",
        )


def test_a_chunk_cannot_declare_an_inverted_window() -> None:
    with pytest.raises(ValueError, match="window ends at"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=360.0,
            window_end=10.0,
            start=0.0,
            end=10.0,
            segment_indices=[0],
            segments=[_segment(0, 0.0, 10.0)],
            text="x",
        )


def test_a_chunk_cannot_declare_an_inverted_extent() -> None:
    with pytest.raises(ValueError, match="at or before its start"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=0.0,
            window_end=360.0,
            start=10.0,
            end=5.0,
            segment_indices=[0],
            segments=[_segment(0, 0.0, 10.0)],
            text="x",
        )


def test_a_chunk_cannot_claim_an_end_its_segments_do_not_reach() -> None:
    with pytest.raises(ValueError, match="latest segment end"):
        TranscriptChunk(
            id="chunk_0000",
            index=0,
            window_start=0.0,
            window_end=360.0,
            start=0.0,
            end=99.0,
            segment_indices=[0],
            segments=[_segment(0, 0.0, 10.0)],
            text="x",
        )


def test_the_chunk_reports_its_own_duration() -> None:
    chunk = build_chunks(_transcript([_segment(0, 10.0, 25.0)]), BASELINE)[0]

    assert chunk.duration_seconds == 15.0
