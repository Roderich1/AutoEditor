"""Transcript chunking (CE-023).

Cuts a transcript into overlapping windows an analyzer can be asked about one at
a time. Two properties matter more than anything else here.

Segments are atomic. A window is an arithmetic boundary, not a semantic one, and
cutting a sentence in half to satisfy it would hand the analyzer material no
human could judge either. A segment overlapping a window is included whole, which
is why the chunk's real extent can reach past its nominal window on both sides.

Timestamps stay absolute. Nothing is rebased to a chunk-local zero at any point.
Every candidate the analyzer proposes must be expressible in source time without
a translation step, because a translation step is somewhere an off-by-one becomes
a clip that starts in the wrong place.
"""

from __future__ import annotations

from content_engine.config import ChunkingSettings
from content_engine.domain.candidates import (
    CHUNKS_SCHEMA_VERSION,
    ChunkCollection,
    TranscriptChunk,
)
from content_engine.domain.models import Transcript, TranscriptSegment
from content_engine.utils.canonical import canonical_sha256

CHUNKS_FILENAME = "chunks.json"

#: Bumped whenever the windowing rule or the rendered text format changes.
#: Both change what the analyzer sees, so both change what it returns.
CHUNKING_RULES_VERSION = 1


def _render(segments: list[TranscriptSegment]) -> str:
    """Render segments with their absolute timestamps in front of them.

    Fixed format, versioned by CHUNKING_RULES_VERSION: how the times are
    presented is part of what the model is asked, so changing it is a change to
    the experiment and has to invalidate the fingerprint that identifies it.
    """
    return "\n".join(
        f"[{segment.start:9.2f} --> {segment.end:9.2f}] {segment.text}" for segment in segments
    )


def _covers(previous: TranscriptChunk, indices: list[int]) -> bool:
    return set(indices) <= set(previous.segment_indices)


def build_chunks(transcript: Transcript, settings: ChunkingSettings) -> list[TranscriptChunk]:
    """Cut a transcript into overlapping windows.

    An empty transcript produces no chunks. That is a legitimate outcome, not an
    error: audio with no recognisable speech is a real thing to hand this
    pipeline, and it should end with zero candidates rather than an exception.
    """
    stride = settings.window_seconds - settings.overlap_seconds
    # Guaranteed by ChunkingSettings, which refuses overlap >= window. Asserted
    # here because a zero stride would loop forever rather than fail.
    if stride <= 0:  # pragma: no cover - unreachable through validated settings
        raise ValueError(f"chunk stride must be positive, got {stride}")

    chunks: list[TranscriptChunk] = []
    window_start = 0.0
    while window_start < transcript.duration_seconds:
        window_end = window_start + settings.window_seconds
        indices = [
            position
            for position, segment in enumerate(transcript.segments)
            if segment.end > window_start and segment.start < window_end
        ]
        if indices:
            # A trailing window that adds nothing the previous one did not
            # already contain would be a second call about the same material.
            if chunks and _covers(chunks[-1], indices):
                break
            segments = [transcript.segments[position] for position in indices]
            chunks.append(
                TranscriptChunk(
                    id=f"chunk_{len(chunks):04d}",
                    index=len(chunks),
                    window_start=window_start,
                    window_end=window_end,
                    start=min(segment.start for segment in segments),
                    end=max(segment.end for segment in segments),
                    segment_indices=indices,
                    segments=segments,
                    text=_render(segments),
                )
            )
        window_start += stride
    return chunks


def transcript_sha256(transcript: Transcript) -> str:
    """Identify the exact transcript a set of chunks was cut from."""
    return canonical_sha256(transcript.model_dump(mode="json"))


def build_chunk_collection(transcript: Transcript, settings: ChunkingSettings) -> ChunkCollection:
    """The chunks plus everything needed to tell where they came from."""
    return ChunkCollection(
        schema_version=CHUNKS_SCHEMA_VERSION,
        rules_version=CHUNKING_RULES_VERSION,
        window_seconds=settings.window_seconds,
        overlap_seconds=settings.overlap_seconds,
        transcript_sha256=transcript_sha256(transcript),
        source_duration_seconds=transcript.duration_seconds,
        chunks=build_chunks(transcript, settings),
    )
