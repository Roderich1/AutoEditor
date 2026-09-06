"""The one test that actually calls Gemini. Off unless explicitly switched on.

Two conditions must both hold before this runs: a credential must be present,
and `CONTENT_ENGINE_RUN_AI_TESTS=1` must be set. The second is the important
one. Gating on the key alone would mean that anyone who had configured the
project for normal use would start paying for the test suite without ever
asking to, and a paid call is not something a developer should discover after
the fact.

It makes exactly one call, against one small chunk, and reports what that cost
in calls, tokens and seconds. It asserts the contract rather than the content: a
model is free to find nothing interesting in twenty seconds of speech, and a
test that demanded candidates would fail for a correct answer.

Everything comes from the configuration rather than from module constants. The
prompt is whatever `analysis.prompt_version` selects, and the identity asserted
is that prompt's own — pinning `clip_candidates/v1` here would keep passing
against a profile that had moved to `v2`, which is the exact failure the
selector exists to prevent.

Nothing here prints a response body, a transcript or a credential. What comes
back is derived from a recording that may be anybody's; the diagnostics report
counts, lengths, durations and model identifiers, all of which are safe to put
in a log.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from content_engine.adapters.analysis.gemini_analyzer import build_gemini_analyzer
from content_engine.adapters.analysis.prompt import select_prompt
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
    # Resolved from the configuration, so this test follows a profile that
    # selects a different prompt instead of silently testing v1 forever.
    prompt = select_prompt(settings.analysis.prompt_version)
    analyzer = build_gemini_analyzer(settings, prompt)

    chunking = ChunkingSettings(window_seconds=360, overlap_seconds=30)
    chunk = build_chunks(speech_transcript(), chunking)[0]
    candidates_policy = settings.analysis.candidates
    context = AnalysisContext(
        min_duration_seconds=candidates_policy.min_duration_seconds,
        max_duration_seconds=candidates_policy.max_duration_seconds,
        run_target_candidates=candidates_policy.target_candidates,
        # From the selected prompt, not from a global that could be pinned to v1.
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )

    started = time.monotonic()
    batch = analyzer.find_candidates(chunk, context)
    elapsed = time.monotonic() - started

    # The cost of this test, stated plainly, because it is a real one. Counts,
    # durations and model identifiers only: no credential, no transcript text
    # and no part of the model's answer.
    print(
        f"\ngemini calls={analyzer.calls} "
        f"prompt_tokens={analyzer.prompt_tokens} "
        f"response_tokens={analyzer.response_tokens} "
        f"seconds={elapsed:.1f} "
        f"requested_model={analyzer.model} "
        f"reported_models={sorted(analyzer.reported_models) or 'none reported'} "
        f"prompt={prompt.configured}->{prompt.version} "
        f"candidates={len(batch.candidates)} "
        f"raw_response_chars={len(batch.raw_response)}"
    )

    assert analyzer.calls == 1, "one chunk must cost exactly one call"
    assert batch.chunk_id == chunk.id
    assert batch.model == settings.analysis.model
    assert analyzer.identity.fixture_sha256 is None
    assert analyzer.identity.prompt.version == prompt.version
    assert analyzer.identity.prompt.sha256 == prompt.sha256
    assert analyzer.identity.uses_packaged_prompt

    # Whatever the provider reported must be the model that was asked for, or a
    # dated build of it. The adapter already refuses anything else; this records
    # what was actually seen, which is the evidence ADR-027 is waiting for.
    for reported in analyzer.reported_models:
        assert reported == analyzer.model or reported.startswith(f"{analyzer.model}-")

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
