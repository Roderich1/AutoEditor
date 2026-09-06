"""The content analysis boundary (CE-027).

There is no implementation to exercise yet, so what is under test is the shape
of the contract and the promise it makes: that nothing above the adapter
boundary can learn which provider is behind it.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import UTC, datetime
from pathlib import Path

from content_engine.domain.candidates import (
    CandidateScores,
    RawCandidate,
    TranscriptChunk,
)
from content_engine.domain.enums import ClipCategory
from content_engine.domain.models import TranscriptSegment
from content_engine.ports import analyzer as analyzer_module
from content_engine.ports.analyzer import (
    AnalysisContext,
    CandidateBatch,
    ContentAnalyzerPort,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2].joinpath("src", "content_engine")

#: A provider SDK may be imported here and nowhere else. Empty today: no adapter
#: exists, which is exactly what this pull request claims.
PROVIDER_MODULES = ("openai", "google", "google.genai", "anthropic")

#: The single file permitted to import a provider SDK. ADR-019 puts Google's
#: types behind the adapter boundary; this is that boundary, by name.
SDK_OWNER = "gemini_analyzer.py"


def _chunk() -> TranscriptChunk:
    segment = TranscriptSegment(index=0, start=0.0, end=30.0, text="hola", words=[])
    return TranscriptChunk(
        id="chunk_0000",
        index=0,
        window_start=0.0,
        window_end=360.0,
        start=0.0,
        end=30.0,
        segment_indices=[0],
        segments=[segment],
        text="[     0.00 -->     30.00] hola",
    )


def _context() -> AnalysisContext:
    return AnalysisContext(
        min_duration_seconds=20.0,
        max_duration_seconds=90.0,
        run_target_candidates=10,
        prompt_version="v1",
        prompt_sha256="a" * 64,
    )


class _StubAnalyzer:
    """Satisfies the port using domain types only, as any adapter must."""

    def find_candidates(self, chunk: TranscriptChunk, context: AnalysisContext) -> CandidateBatch:
        candidate = RawCandidate(
            start=chunk.start,
            end=chunk.start + context.min_duration_seconds,
            category=ClipCategory.EXPLANATION,
            topic="tema",
            hook="gancho",
            summary="resumen",
            reason="motivo",
            scores=CandidateScores(
                hook=70,
                value=70,
                context_independence=70,
                clarity=70,
                engagement_potential=70,
                relevance=70,
            ),
        )
        return CandidateBatch(
            chunk_id=chunk.id,
            candidates=(candidate,),
            raw_response="{}",
            model="stub",
        )


def test_a_domain_only_implementation_satisfies_the_port() -> None:
    analyzer: ContentAnalyzerPort = _StubAnalyzer()

    batch = analyzer.find_candidates(_chunk(), _context())

    assert batch.chunk_id == "chunk_0000"
    assert len(batch.candidates) == 1
    assert batch.candidates[0].start == 0.0


def test_the_port_is_a_protocol_with_one_method() -> None:
    assert getattr(ContentAnalyzerPort, "_is_protocol", False)
    methods = [
        name
        for name in dir(ContentAnalyzerPort)
        if not name.startswith("_") and callable(getattr(ContentAnalyzerPort, name, None))
    ]
    assert methods == ["find_candidates"]


def test_the_context_is_immutable() -> None:
    """Instruction to the analyzer is settled before the call, not during it."""
    context = _context()

    assert type(context).__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert type(CandidateBatch).__name__ == "type"


def test_the_batch_keeps_the_raw_response() -> None:
    """A parse failure must still leave evidence of what the provider said."""
    batch = _StubAnalyzer().find_candidates(_chunk(), _context())

    assert batch.raw_response == "{}"
    assert batch.model == "stub"


def test_the_signature_mentions_no_provider_type() -> None:
    signature = inspect.signature(ContentAnalyzerPort.find_candidates)
    rendered = str(signature)

    for module in PROVIDER_MODULES:
        assert module not in rendered.lower()


def test_the_port_module_imports_no_provider_sdk() -> None:
    tree = ast.parse(Path(analyzer_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not imported & {module.split(".")[0] for module in PROVIDER_MODULES}


def test_only_the_gemini_adapter_imports_a_provider_sdk() -> None:
    """The SDK exists now, and exactly one file in the package may see it.

    This was "no module imports a provider" for as long as there was no
    adapter. The boundary it was protecting never changed: the domain, the
    ports, the services and the CLI describe candidates in their own types, and
    a provider import anywhere among them is what would make swapping providers
    a rewrite instead of an experiment. So the assertion narrows to a single
    named file rather than disappearing.
    """
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path.name == SDK_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {m.split(".")[0] for m in PROVIDER_MODULES}:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert offenders == []


def test_the_one_file_allowed_to_import_the_sdk_still_exists() -> None:
    """Otherwise the exemption above would silently protect nothing.

    A rename would make the skip match no file, every real import would move
    somewhere unexamined, and the suite would stay green while the boundary was
    gone.
    """
    owner = SOURCE_ROOT.joinpath("adapters", "analysis", SDK_OWNER)

    assert owner.is_file()
    assert "google" in owner.read_text(encoding="utf-8")


def test_the_analysis_context_carries_the_prompt_identity() -> None:
    """Every run records which prompt produced it; the port is where it enters."""
    context = _context()

    assert context.prompt_version == "v1"
    assert len(context.prompt_sha256) == 64


def test_the_target_is_named_for_the_run_not_the_chunk() -> None:
    """One semantics only: the objective is global, never a per-call quota.

    The field is checked by name because the ambiguity this replaces was purely
    one of naming: a field called target_candidates on a per-chunk call reads as
    "return this many", and CandidateCollection means something else by it.
    """
    fields = {field.name for field in dataclasses.fields(AnalysisContext)}

    assert "run_target_candidates" in fields
    assert "target_candidates" not in fields


def test_the_context_carries_no_per_chunk_budget_yet() -> None:
    """A per-chunk budget would be a separate computed field, not this one."""
    fields = {field.name for field in dataclasses.fields(AnalysisContext)}

    assert not any(name.startswith("chunk_") for name in fields)


def test_the_batch_is_immutable_and_holds_a_tuple() -> None:
    batch = CandidateBatch(chunk_id="chunk_0000", candidates=(), raw_response="", model="m")

    assert type(batch).__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert isinstance(batch.candidates, tuple)
    assert datetime.now(UTC).tzinfo is UTC
