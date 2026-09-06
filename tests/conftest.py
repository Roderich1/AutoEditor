from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.analysis.fixture_analyzer import AnalysisFixture, FixtureBatch
from content_engine.adapters.persistence.filesystem import RunWorkspace
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
from content_engine.domain.enums import ClipCategory, RunStatus
from content_engine.domain.exceptions import EXIT_SUCCESS
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
from content_engine.services.analysis_service import ARTIFACT_FILENAMES, CANDIDATES_FILENAME
from content_engine.services.run_service import RunService


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


def weak_candidate(start: float, end: float, **overrides: Any) -> RawCandidate:
    """A proposal no deterministic rule can rescue: it scores 10 and min_score is 65."""
    payload: dict[str, Any] = {
        "scores": CandidateScores(
            hook=10,
            value=10,
            context_independence=10,
            clarity=10,
            engagement_potential=10,
            relevance=10,
        )
    }
    payload.update(overrides)
    return raw_candidate(start, end, **payload)


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


#: Every SGR/CSI sequence rich may emit. Matched rather than assumed absent,
#: because colour is decided by the environment and not by this suite.
ANSI = re.compile(r"\[[0-9;]*[A-Za-z]")


def cli_output(result: object) -> str:
    """CLI output with styling removed and whitespace collapsed.

    Two things vary between a developer terminal and a CI runner, and neither is
    anything a test means to assert on.

    Rich wraps to the console width, so a phrase that fits on one line locally is
    split mid-sentence elsewhere. Hence the whitespace collapse.

    Rich also emits colour when the environment asks for it, and it styles parts
    of a token independently: an option renders as `-` `-fixture` with an escape
    sequence between the two dashes, so a plain `"--fixture" in output` is true
    without colour and false with it. That is exactly the kind of failure that
    only appears in CI, and it did. Stripping the escapes first makes the
    assertion about what the command said rather than about how it was painted.
    """
    return " ".join(ANSI.sub("", str(getattr(result, "output", result))).split())


# --- the analyze harness -----------------------------------------------------
#
# A fully transcribed run, built through the real CLI with fake FFmpeg and a
# fake transcriber. It lives here rather than in one test module because both
# the fixture-mode and the provider-mode command tests need the same starting
# point, and two copies of a setup this long drift the moment one is edited.

AUDIO_DURATION = 119.0


@dataclass
class Harness:
    run_id: str
    run_path: Path
    fixture_path: Path
    tmp_path: Path

    @property
    def analysis(self) -> Path:
        return self.run_path.joinpath("analysis")

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.run_path.joinpath("manifest.json").read_text(encoding="utf-8"))

    def candidates(self) -> dict[str, Any]:
        return json.loads(self.analysis.joinpath(CANDIDATES_FILENAME).read_text(encoding="utf-8"))

    def snapshot(self) -> dict[str, bytes]:
        return {
            name: self.analysis.joinpath(name).read_bytes()
            for name in ARTIFACT_FILENAMES
            if self.analysis.joinpath(name).is_file()
        } | {"manifest.json": self.run_path.joinpath("manifest.json").read_bytes()}


def _segments(count: int = 12) -> tuple:
    from tests.conftest import raw_segment, raw_word

    return tuple(
        raw_segment(
            index * 10.0,
            index * 10.0 + 9.0,
            f"segmento {index}",
            (
                raw_word("hola", index * 10.0, index * 10.0 + 0.5),
                raw_word("mundo", index * 10.0 + 8.5, index * 10.0 + 9.0),
            ),
        )
        for index in range(count)
    )


DEFAULT_BATCH = FixtureBatch(
    chunk_id="chunk_0000",
    raw_response='{"candidates": [{"start": 10.2, "end": 39.4}]}',
    candidates=[raw_candidate(10.2, 39.4), raw_candidate(60.0, 85.0, hook=70)],
)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    from tests.conftest import raw_transcription

    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("workspace")))
    monkeypatch.delenv("CONTENT_ENGINE_ANALYSIS_MODEL", raising=False)
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )

    def fake_probe(arguments: Sequence[str], timeout: float | None = None) -> Any:
        payload = {
            "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            "format": {"duration": str(AUDIO_DURATION), "format_name": "wav"},
        }
        return fake_process(arguments, json.dumps(payload))

    monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", fake_probe)

    video = tmp_path.joinpath("sample.mp4")
    video.write_bytes(b"video")
    settings = load_settings()
    workspace = RunWorkspace(settings.workspace.root)
    service = RunService(settings, workspace)
    run_path, manifest = service.create(video)
    manifest = service.advance(run_path, manifest, RunStatus.INSPECTED)
    run_path.joinpath("audio", "source.wav").write_bytes(b"wav-bytes")
    service.advance(run_path, manifest, RunStatus.AUDIO_READY)

    transcriber = FakeTranscriber(raw_transcription(_segments(), AUDIO_DURATION))
    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: transcriber)
    assert CliRunner().invoke(cli.app, ["transcribe", run_path.name]).exit_code == EXIT_SUCCESS

    fixture_path = write_fixture(
        tmp_path.joinpath("fixture.json"), analysis_fixture([DEFAULT_BATCH])
    )
    return Harness(run_path.name, run_path, fixture_path, tmp_path)


# --- the preview harness -----------------------------------------------------
#
# Preview generation runs a real adapter against a fake encoder: the argument
# list is built by the production code and asserted, while the "encode" writes a
# deterministic placeholder and remembers what ffprobe should later say about
# it. That keeps the unit suite free of FFmpeg without letting the command under
# test be a stand-in for itself. Real FFmpeg is exercised in tests/integration.

PREVIEW_WIDTH = 540
PREVIEW_HEIGHT = 960


@dataclass
class FakeMedia:
    """A fake encoder and the ffprobe answers about what it produced."""

    width: int = PREVIEW_WIDTH
    height: int = PREVIEW_HEIGHT
    audio: bool = True
    calls: list[list[str]] = field(default_factory=list)
    probed: list[Path] = field(default_factory=list)
    #: Output basenames the encoder must refuse, so a failure can be placed.
    fail_for: set[str] = field(default_factory=set)
    #: Output basename -> duration ffprobe will report, overriding the request.
    measured: dict[str, float] = field(default_factory=dict)
    #: Output basename -> the dimensions ffprobe will report.
    dimensions: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: What ffprobe will name the video codec, so a wrong encode can be placed.
    video_codec: str = "h264"
    _known: dict[Path, dict[str, Any]] = field(default_factory=dict)

    def ffmpeg(self, arguments: Sequence[str], timeout: float | None = None) -> Any:
        from content_engine.domain.exceptions import ExternalToolError

        self.calls.append(list(arguments))
        output = Path(arguments[-1])
        if output.name in self.fail_for:
            raise ExternalToolError(f"ffmpeg failed: synthetic refusal of {output.name}")
        requested = float(arguments[arguments.index("-t") + 1])
        width, height = self.dimensions.get(output.name, (self.width, self.height))
        duration = self.measured.get(output.name, requested)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"preview:{output.name}:{requested:.3f}".encode())
        streams: list[dict[str, Any]] = [
            {
                "codec_type": "video",
                "codec_name": self.video_codec,
                "width": width,
                "height": height,
                "avg_frame_rate": "30/1",
            }
        ]
        if self.audio:
            streams.append(
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "channels": 2,
                }
            )
        self._known[output.resolve()] = {
            "streams": streams,
            "format": {"duration": f"{duration:.3f}", "format_name": "mov,mp4,m4a"},
        }
        return fake_process(arguments)

    def ffprobe(self, arguments: Sequence[str], timeout: float | None = None) -> Any:
        from content_engine.domain.exceptions import ExternalToolError

        path = Path(arguments[-1])
        self.probed.append(path)
        if path.suffix == ".wav":
            payload: dict[str, Any] = {
                "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
                "format": {"duration": str(AUDIO_DURATION), "format_name": "wav"},
            }
            return fake_process(arguments, json.dumps(payload))
        known = self._known.get(path.resolve())
        if known is None:
            raise ExternalToolError(f"ffprobe failed: {path} was never produced")
        return fake_process(arguments, json.dumps(known))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
        monkeypatch.setattr("content_engine.adapters.media.preview.run_command", self.ffmpeg)
        monkeypatch.setattr("content_engine.adapters.media.ffprobe.run_command", self.ffprobe)
        return self

    def forget(self, path: Path) -> None:
        """Make ffprobe deny a file it previously described."""
        self._known.pop(path.resolve(), None)


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    return FakeMedia().install(monkeypatch)


@dataclass
class Analysed:
    """An analysed run plus the pieces preview and review need from it."""

    harness: Harness
    media: FakeMedia

    @property
    def run_id(self) -> str:
        return self.harness.run_id

    @property
    def run_path(self) -> Path:
        return self.harness.run_path

    @property
    def previews(self) -> Path:
        return self.run_path.joinpath("previews")

    @property
    def review(self) -> Path:
        return self.run_path.joinpath("review")

    def manifest(self) -> dict[str, Any]:
        return self.harness.manifest()

    def candidates(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = self.harness.candidates()["candidates"]
        return payload

    def analysis_fingerprint(self) -> str:
        fingerprint: str = self.manifest()["stages"]["analysis"]["fingerprint"]
        return fingerprint

    def preview_snapshot(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in sorted(self.previews.iterdir())
            if path.is_file()
        }

    def decisions(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(
            self.review.joinpath("decisions.json").read_text(encoding="utf-8")
        )
        return payload


def analyse(harness: Harness, *arguments: str) -> Any:
    """Run the analyze command over the harness fixture."""
    return CliRunner().invoke(
        cli.app, ["analyze", harness.run_id, "--fixture", str(harness.fixture_path), *arguments]
    )


@pytest.fixture
def analysed(harness: Harness, media: FakeMedia) -> Analysed:
    result = analyse(harness)
    assert result.exit_code == EXIT_SUCCESS, cli_output(result)
    return Analysed(harness, media)
