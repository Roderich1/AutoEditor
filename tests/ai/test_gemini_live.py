"""The one test that actually calls Gemini. Off unless explicitly switched on.

Two conditions must both hold before this runs: a credential must be present,
and `CONTENT_ENGINE_RUN_AI_TESTS=1` must be set. The second is the important
one. Gating on the key alone would mean that anyone who had configured the
project for normal use would start paying for the test suite without ever
asking to, and a paid call is not something a developer should discover after
the fact.

It makes exactly one call, against one small chunk, and reports what that cost
in calls and tokens. It asserts the contract rather than the content: a model is
free to find nothing interesting in twenty seconds of speech, and a test that
demanded candidates would fail for a correct answer.

Nothing here prints a response body. What comes back is derived from a
transcript, and a transcript may be anybody's recording; the assertions work on
counts, types and bounds, and the diagnostics print lengths rather than text.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from content_engine.adapters.analysis.gemini_analyzer import build_gemini_analyzer
from content_engine.adapters.analysis.prompt import PROMPT_SHA256, PROMPT_VERSION
from content_engine.config import ANALYSIS_CREDENTIAL_ENV_VAR, ChunkingSettings, load_settings
from content_engine.domain.enums import ClipCategory
from content_engine.ports.analyzer import AnalysisContext
from content_engine.services.chunking_service import build_chunks
from tests.conftest import speech_transcript

RUN_FLAG = "CONTENT_ENGINE_RUN_AI_TESTS"

pytestmark = [
    pytest.mark.ai,
    pytest.mark.skipif(
        os.getenv(RUN_FLAG) != "1",
        reason=f"set {RUN_FLAG}=1 to spend real quota on this test",
    ),
    pytest.mark.skipif(
        not os.getenv(ANALYSIS_CREDENTIAL_ENV_VAR),
        reason=f"{ANALYSIS_CREDENTIAL_ENV_VAR} is not set",
    ),
]


def test_one_real_call_returns_an_answer_this_build_can_use() -> None:
    settings = load_settings()
    analyzer = build_gemini_analyzer(settings)
    chunk = build_chunks(
        speech_transcript(), ChunkingSettings(window_seconds=360, overlap_seconds=30)
    )[0]
    context = AnalysisContext(
        min_duration_seconds=settings.analysis.candidates.min_duration_seconds,
        max_duration_seconds=settings.analysis.candidates.max_duration_seconds,
        run_target_candidates=settings.analysis.candidates.target_candidates,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256,
    )

    started = time.monotonic()
    batch = analyzer.find_candidates(chunk, context)
    elapsed = time.monotonic() - started

    # The cost of this test, stated plainly, because it is a real one.
    print(
        f"\ngemini calls={analyzer.calls} "
        f"prompt_tokens={analyzer.prompt_tokens} "
        f"response_tokens={analyzer.response_tokens} "
        f"seconds={elapsed:.1f} "
        f"candidates={len(batch.candidates)} "
        f"raw_response_chars={len(batch.raw_response)}"
    )

    assert analyzer.calls == 1, "one chunk must cost exactly one call"
    assert batch.chunk_id == chunk.id
    assert batch.model == settings.analysis.model
    assert analyzer.identity.fixture_sha256 is None

    # The raw response is the evidence the artifact will keep. It must be the
    # JSON the schema asked for, not prose with JSON somewhere inside it.
    payload = json.loads(batch.raw_response)
    assert isinstance(payload, dict)
    assert isinstance(payload["candidates"], list)

    # Zero candidates is a correct answer for a chunk with nothing in it, so
    # what is asserted is the shape of whatever came back, not that it exists.
    for candidate in batch.candidates:
        assert isinstance(candidate.start, float)
        assert isinstance(candidate.end, float)
        assert candidate.category in set(ClipCategory)
        for name, score in candidate.scores.model_dump().items():
            assert 0 <= score <= 100, name
        # Never the text itself: it is derived from somebody's recording.
        assert candidate.topic
        assert candidate.summary
