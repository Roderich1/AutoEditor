from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from content_engine.adapters.analysis.fixture_analyzer import AnalysisFixture, FixtureBatch
from content_engine.config import WORKSPACE_ENV_VAR, Settings, load_settings
from content_engine.domain.candidate_rules import (
    CandidatePolicy,
    PromptIdentity,
    Proposal,
    candidate_id,
    select_candidates,
)
from content_engine.domain.candidates import (
    CandidateCollection,
    CandidateScores,
    RawCandidate,
    TranscriptChunk,
)
from content_engine.domain.enums import ClipCategory
from content_engine.domain.models import (
    RawSegment,
    RawTranscription,
    RawWord,
    ResolvedHardware,
    Transcript,
    TranscriptionOptions,
    TranscriptSegment,
    TranscriptWord,
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


# --- candidate engine builders ----------------------------------------------
#
# One transcript shape is used by every candidate test that does not need
# something unusual: twelve nine-second segments starting at multiples of ten,
# two words each, in 119 seconds of audio. The ten-second grid makes every
# expected snap target readable in the test itself rather than computed, and the
# one-second silence between segments is where an ungrounded timestamp can live.

CANDIDATE_POLICY = CandidatePolicy(
    min_duration_seconds=20.0,
    max_duration_seconds=90.0,
    min_score=65.0,
    target_candidates=10,
    max_candidates=15,
    dedupe_iou=0.60,
    boundary_snap_seconds=2.5,
)

PROMPT = PromptIdentity(version="fake-fixture/v1", sha256="a" * 64)

TRANSCRIPT_SHA = "f" * 64


def transcript_segment(
    index: int, start: float, end: float, *, words: bool = True
) -> TranscriptSegment:
    spoken = (
        [
            TranscriptWord(word="hola", start=start, end=start + 0.5, probability=0.9),
            TranscriptWord(word="mundo", start=end - 0.5, end=end, probability=0.9),
        ]
        if words
        else []
    )
    return TranscriptSegment(
        index=index, start=start, end=end, text=f"segmento {index}", words=spoken
    )


def speech_transcript(count: int = 12, *, words: bool = True) -> Transcript:
    segments = [
        transcript_segment(index, index * 10.0, index * 10.0 + 9.0, words=words)
        for index in range(count)
    ]
    return Transcript(
        language="es",
        language_probability=0.99,
        duration_seconds=(count - 1) * 10.0 + 9.0,
        declared_duration_seconds=(count - 1) * 10.0 + 9.0,
        segments=segments,
        model="fake-model",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def chunk_of(transcript: Transcript, chunk_id: str = "chunk_0000") -> TranscriptChunk:
    """One chunk holding the whole transcript, for rule tests that need no windowing."""
    return TranscriptChunk(
        id=chunk_id,
        index=0,
        window_start=0.0,
        window_end=360.0,
        start=transcript.segments[0].start,
        end=transcript.segments[-1].end,
        segment_indices=[segment.index for segment in transcript.segments],
        segments=list(transcript.segments),
        text="rendered",
    )


def scores(hook: int = 92) -> CandidateScores:
    return CandidateScores(
        hook=hook,
        value=88,
        context_independence=96,
        clarity=93,
        engagement_potential=84,
        relevance=90,
    )


def raw_candidate(start: float, end: float, hook: int = 92, **overrides: Any) -> RawCandidate:
    payload: dict[str, Any] = {
        "start": start,
        "end": end,
        "category": ClipCategory.EXPLANATION,
        "topic": "tema",
        "hook": "gancho",
        "summary": "resumen",
        "reason": "motivo",
        "scores": scores(hook),
    }
    payload.update(overrides)
    return RawCandidate(**payload)


def proposals_of(
    chunk: TranscriptChunk,
    candidates: Sequence[RawCandidate],
    transcript_sha: str = TRANSCRIPT_SHA,
) -> list[Proposal]:
    return [
        Proposal(
            id=candidate_id(transcript_sha, chunk.id, ordinal, raw, PROMPT),
            chunk=chunk,
            ordinal=ordinal,
            raw=raw,
        )
        for ordinal, raw in enumerate(candidates)
    ]


def collect(
    chunk: TranscriptChunk,
    candidates: Sequence[RawCandidate],
    policy: CandidatePolicy = CANDIDATE_POLICY,
    source_duration_seconds: float = 119.0,
) -> CandidateCollection:
    return select_candidates(
        proposals_of(chunk, candidates),
        source_duration_seconds,
        policy,
        1,
        datetime(2026, 1, 1, tzinfo=UTC),
    )


def analysis_fixture(
    batches: Sequence[FixtureBatch], model: str = "fake-fixture-model"
) -> AnalysisFixture:
    return AnalysisFixture(schema_version=1, model=model, batches=list(batches))


def write_fixture(path: Path, fixture: AnalysisFixture) -> Path:
    path.write_text(
        json.dumps(fixture.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def cli_output(result: object) -> str:
    """CLI output with newlines and runs of spaces collapsed to single spaces.

    Rich wraps to the console width, and that width differs between a developer
    terminal and a CI runner, so a phrase that fits on one line locally can be
    split mid-sentence elsewhere. Asserting on the raw text makes a test depend
    on terminal geometry rather than on what the command said.
    """
    return " ".join(str(getattr(result, "output", result)).split())
