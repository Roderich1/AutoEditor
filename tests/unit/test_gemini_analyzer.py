"""CE-028: the Gemini adapter, exercised entirely against doubles.

Nothing here opens a socket. The adapter is handed a stand-in for the SDK
client, which records the request it was given and returns whatever the test
decided the provider said. That is the only way to assert the things that
actually matter about this boundary — that the system instruction is separate
from the speech, that no tool is ever enabled, that a 429 is retried and a 401
is not, that a credential never reaches a message — without paying for a call
or depending on a model's mood.

The request objects are real SDK types, built by the real adapter code. A test
double for the transport keeps the network out; a double for the request would
have let a wrong parameter name pass unnoticed until the first live call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from content_engine.adapters.analysis.gemini_analyzer import (
    ANALYZER_NAME,
    MAX_ATTEMPTS,
    GeminiContentAnalyzer,
    build_gemini_analyzer,
)
from content_engine.adapters.analysis.prompt import (
    PROMPT_SHA256,
    PROMPT_TEXT,
    PROMPT_VERSION,
    select_prompt,
)
from content_engine.config import ChunkingSettings, Settings
from content_engine.domain.candidates import TranscriptChunk
from content_engine.domain.exceptions import (
    EXIT_ANALYSIS,
    EXIT_CONFIGURATION,
    AnalysisError,
    ConfigurationError,
)
from content_engine.ports.analyzer import AnalysisContext
from content_engine.services.chunking_service import build_chunks
from tests.conftest import speech_transcript

MODEL = "gemini-3.5-flash-lite"
SECRET = "AIzaSy-not-a-real-key-0123456789"

SCORES = {
    "hook": 80,
    "value": 75,
    "context_independence": 70,
    "clarity": 85,
    "engagement_potential": 60,
    "relevance": 90,
}


def answer(*candidates: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(candidates)}, ensure_ascii=False)


def proposal(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "start": 12.0,
        "end": 48.0,
        "category": "explanation",
        "topic": "systemd",
        "hook": "Casi nadie sabe esto de systemd",
        "summary": "Explica qué hace systemd al arrancar.",
        "reason": "Idea completa con inicio y cierre.",
        "scores": dict(SCORES),
    }
    base.update(overrides)
    return base


# --- doubles -----------------------------------------------------------------


@dataclass
class FakeUsage:
    prompt_token_count: int = 1200
    candidates_token_count: int = 300
    total_token_count: int = 1500


@dataclass
class FakeCandidate:
    finish_reason: Any = "STOP"


@dataclass
class FakeResponse:
    text: str | None
    model_version: str | None = MODEL
    candidates: list[FakeCandidate] = field(default_factory=lambda: [FakeCandidate()])
    prompt_feedback: Any = None
    usage_metadata: Any = field(default_factory=FakeUsage)


@dataclass
class FakeModels:
    """Records every request and replays a scripted list of outcomes."""

    outcomes: list[Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.requests.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes[min(len(self.requests) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class FakeClient:
    models: FakeModels


def client_returning(*outcomes: Any) -> FakeClient:
    return FakeClient(models=FakeModels(outcomes=list(outcomes)))


#: The prompt every test here sends: the real selection, so a change to the
#: selector is caught rather than routed around by a hand-made stand-in.
PROMPT = select_prompt("v1")


def analyzer_for(client: FakeClient, model: str = MODEL) -> GeminiContentAnalyzer:
    """Sleep is replaced so a retry test does not actually wait."""
    return GeminiContentAnalyzer(
        model=model, client=client, prompt=PROMPT, sleep=lambda _seconds: None
    )


@pytest.fixture
def chunk() -> TranscriptChunk:
    """A real chunk, rendered by the real chunker.

    Built rather than hand-written so the text the adapter sends is the text the
    pipeline really produces, timestamp prefixes included.
    """
    chunking = ChunkingSettings(window_seconds=360, overlap_seconds=30)
    return build_chunks(speech_transcript(), chunking)[0]


@pytest.fixture
def context() -> AnalysisContext:
    return AnalysisContext(
        min_duration_seconds=20.0,
        max_duration_seconds=90.0,
        run_target_candidates=10,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256,
    )


# --- the request ------------------------------------------------------------


def test_the_configured_model_is_the_one_requested(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    assert client.models.requests[0]["model"] == MODEL


def test_the_system_instruction_is_the_packaged_prompt_and_holds_no_speech(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """The trusted half. It is the hashed prompt and nothing else."""
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    instruction = client.models.requests[0]["config"].system_instruction
    assert instruction == PROMPT_TEXT
    assert chunk.text not in instruction


def test_the_transcript_travels_as_content_not_as_instruction(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    sent = json.dumps(_contents_text(client.models.requests[0]["contents"]))
    assert chunk.segments[0].text in sent


def test_the_request_states_the_duration_policy_and_the_run_objective(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    parameters = _parameters(client.models.requests[0]["contents"])
    assert parameters["min_duration_seconds"] == 20.0
    assert parameters["max_duration_seconds"] == 90.0
    assert parameters["run_target_candidates"] == 10
    assert parameters["chunk_start_seconds"] == chunk.start
    assert parameters["chunk_end_seconds"] == chunk.end


def test_the_run_objective_is_named_for_the_run_not_for_the_chunk(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """ADR-021. A field called `candidates_wanted` here would invite the bug."""
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    parameters = _parameters(client.models.requests[0]["contents"])
    assert "run_target_candidates" in parameters
    assert not any(key.startswith("chunk_target") for key in parameters)


def test_structured_output_is_requested_against_the_declared_schema(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    config = client.models.requests[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None


def test_no_tool_search_or_code_execution_is_ever_enabled(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """The transcript is untrusted, so the model is given nothing to act with."""
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    config = client.models.requests[0]["config"]
    assert config.tools is None
    assert config.tool_config is None
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True


def test_no_local_path_or_media_is_sent(chunk: TranscriptChunk, context: AnalysisContext) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer_for(client).find_candidates(chunk, context)
    sent = json.dumps(_contents_text(client.models.requests[0]["contents"]))
    for forbidden in (".mp4", ".wav", "workspace", "samples-local", "C:\\\\"):
        assert forbidden not in sent


# --- the answer --------------------------------------------------------------


def test_a_valid_answer_becomes_a_candidate_batch(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer(proposal())))
    batch = analyzer_for(client).find_candidates(chunk, context)
    assert batch.chunk_id == chunk.id
    assert batch.model == MODEL
    assert len(batch.candidates) == 1
    assert batch.candidates[0].topic == "systemd"


def test_the_raw_response_is_preserved_exactly(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    text = answer(proposal())
    client = client_returning(FakeResponse(text=text))
    batch = analyzer_for(client).find_candidates(chunk, context)
    assert batch.raw_response == text


def test_an_empty_candidate_list_is_a_successful_call(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    batch = analyzer_for(client).find_candidates(chunk, context)
    assert batch.candidates == ()


def test_the_identity_names_gemini_the_real_prompt_and_no_fixture() -> None:
    identity = analyzer_for(client_returning()).identity
    assert identity.analyzer == ANALYZER_NAME == "gemini"
    assert identity.model == MODEL
    assert identity.prompt.version == PROMPT_VERSION
    assert identity.prompt.sha256 == PROMPT_SHA256
    assert identity.fixture_sha256 is None


def test_no_google_type_escapes_the_adapter(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """Everything returned belongs to the domain, so the port stays neutral."""
    client = client_returning(FakeResponse(text=answer(proposal())))
    batch = analyzer_for(client).find_candidates(chunk, context)
    for value in (batch, batch.candidates[0], batch.candidates[0].scores):
        assert not type(value).__module__.startswith("google")


# --- refusals ----------------------------------------------------------------


def test_an_empty_response_is_an_analysis_failure(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=None))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    assert caught.value.exit_code == EXIT_ANALYSIS


def test_a_truncated_answer_names_the_finish_reason(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(
        FakeResponse(
            text='{"candidates": [', candidates=[FakeCandidate(finish_reason="MAX_TOKENS")]
        )
    )
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError, match="MAX_TOKENS"):
        analyzer.find_candidates(chunk, context)


def test_a_blocked_prompt_is_an_analysis_failure(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    blocked = FakeResponse(text=None, prompt_feedback=_Blocked("SAFETY"))
    client = client_returning(blocked)
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError, match="SAFETY"):
        analyzer.find_candidates(chunk, context)


def test_a_different_model_answering_is_refused_when_it_is_verifiable(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer(), model_version="gemini-2.0-pro"))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError, match="gemini-2.0-pro"):
        analyzer.find_candidates(chunk, context)


def test_a_dated_build_of_the_requested_model_is_accepted(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """`gemini-3.5-flash-lite-001` is the model asked for, not another one."""
    client = client_returning(FakeResponse(text=answer(), model_version=f"{MODEL}-001"))
    batch = analyzer_for(client).find_candidates(chunk, context)
    assert batch.model == MODEL


def test_an_unreported_model_version_is_not_treated_as_a_mismatch(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer(), model_version=None))
    assert analyzer_for(client).find_candidates(chunk, context).candidates == ()


# --- retries -----------------------------------------------------------------


def test_a_rate_limit_is_retried_and_can_succeed(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    from google.genai import errors

    limited = errors.ClientError(
        429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    )
    client = FakeClient(
        models=FakeModels(outcomes=[limited, FakeResponse(text=answer(proposal()))])
    )
    batch = analyzer_for(client).find_candidates(chunk, context)
    assert len(batch.candidates) == 1
    assert len(client.models.requests) == 2


def test_a_server_error_is_retried(chunk: TranscriptChunk, context: AnalysisContext) -> None:
    from google.genai import errors

    broken = errors.ServerError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    client = FakeClient(models=FakeModels(outcomes=[broken, FakeResponse(text=answer())]))
    analyzer_for(client).find_candidates(chunk, context)
    assert len(client.models.requests) == 2


def test_a_timeout_is_retried(chunk: TranscriptChunk, context: AnalysisContext) -> None:
    import httpx

    client = FakeClient(
        models=FakeModels(outcomes=[httpx.ReadTimeout("timed out"), FakeResponse(text=answer())])
    )
    analyzer_for(client).find_candidates(chunk, context)
    assert len(client.models.requests) == 2


def test_retries_are_bounded(chunk: TranscriptChunk, context: AnalysisContext) -> None:
    from google.genai import errors

    broken = errors.ServerError(500, {"error": {"message": "boom", "status": "INTERNAL"}})
    client = FakeClient(models=FakeModels(outcomes=[broken]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError):
        analyzer.find_candidates(chunk, context)
    assert len(client.models.requests) == MAX_ATTEMPTS


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_deterministic_client_error_is_never_retried(
    chunk: TranscriptChunk, context: AnalysisContext, code: int
) -> None:
    """Retrying a bad key or a bad request only spends quota to fail again."""
    from google.genai import errors

    refused = errors.ClientError(code, {"error": {"message": "nope", "status": "INVALID_ARGUMENT"}})
    client = FakeClient(models=FakeModels(outcomes=[refused]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError):
        analyzer.find_candidates(chunk, context)
    assert len(client.models.requests) == 1


def test_an_invalid_schema_answer_is_never_retried(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = FakeClient(
        models=FakeModels(outcomes=[FakeResponse(text=answer(proposal(category="x")))])
    )
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError):
        analyzer.find_candidates(chunk, context)
    assert len(client.models.requests) == 1


# --- secrets -----------------------------------------------------------------


def test_a_provider_error_message_never_repeats_a_credential(
    chunk: TranscriptChunk, context: AnalysisContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the provider echoes the key back, it stops at this boundary."""
    from google.genai import errors

    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    leaking = errors.ClientError(
        400, {"error": {"message": f"API key {SECRET} is not valid", "status": "INVALID_ARGUMENT"}}
    )
    client = FakeClient(models=FakeModels(outcomes=[leaking]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    assert SECRET not in str(caught.value)


def test_a_provider_error_does_not_dump_the_sdk_object(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    from google.genai import errors

    refused = errors.ClientError(
        403, {"error": {"message": "denied", "status": "PERMISSION_DENIED"}}
    )
    client = FakeClient(models=FakeModels(outcomes=[refused]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    message = str(caught.value)
    assert "403" in message
    assert "{'error'" not in message
    assert "response_json" not in message


# --- construction ------------------------------------------------------------


def test_building_the_analyzer_without_a_credential_is_a_configuration_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError) as caught:
        build_gemini_analyzer(settings, PROMPT)
    assert caught.value.exit_code == EXIT_CONFIGURATION
    assert "GEMINI_API_KEY" in str(caught.value)


def test_a_blank_credential_is_treated_as_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        build_gemini_analyzer(settings, PROMPT)


def test_a_placeholder_model_is_a_configuration_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    placeholder = settings.model_copy(deep=True)
    placeholder.analysis.model = "SET_MODEL_HERE"
    with pytest.raises(ConfigurationError, match="model"):
        build_gemini_analyzer(placeholder, PROMPT)


def test_the_credential_is_not_read_when_it_is_not_needed(
    monkeypatch: pytest.MonkeyPatch, chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """Constructing the analyzer directly with a client asks for nothing."""

    def explode(name: str, default: Any = None) -> Any:
        raise AssertionError(f"the environment was read for {name}")

    client = client_returning(FakeResponse(text=answer()))
    analyzer = analyzer_for(client)
    monkeypatch.setattr("os.getenv", explode)
    analyzer.find_candidates(chunk, context)


# --- helpers -----------------------------------------------------------------


@dataclass
class _Blocked:
    block_reason: str


def _contents_text(contents: Any) -> list[str]:
    parts = contents if isinstance(contents, list) else [contents]
    texts: list[str] = []
    for part in parts:
        inner = getattr(part, "parts", None)
        if inner is not None:
            texts.extend(item.text or "" for item in inner)
        else:
            texts.append(getattr(part, "text", None) or str(part))
    return texts


def _parameters(contents: Any) -> dict[str, Any]:
    """The operator's JSON block, wherever in the request it was placed."""
    for text in _contents_text(contents):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            continue
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "run_target_candidates" in payload:
            return payload
    raise AssertionError("no request parameter block was sent")


def test_building_the_analyzer_hands_the_credential_to_the_sdk_and_keeps_it_there(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one legitimate use of the variable, and the end of its travels."""
    from google import genai

    captured: dict[str, Any] = {}

    class RecordingClient:
        def __init__(self, *, api_key: str) -> None:
            captured["api_key"] = api_key

    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setattr(genai, "Client", RecordingClient)

    analyzer = build_gemini_analyzer(settings, PROMPT)

    assert captured["api_key"] == SECRET
    assert analyzer.model == settings.analysis.model
    assert analyzer.identity.analyzer == ANALYZER_NAME
    assert analyzer.identity.fixture_sha256 is None
    # Not kept anywhere a later reader could reach it.
    assert SECRET not in repr(vars(analyzer))


def test_an_unrecognised_failure_is_not_retried(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """Only failures known to be transient are worth a second call."""
    client = FakeClient(models=FakeModels(outcomes=[ValueError("something else entirely")]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    assert len(client.models.requests) == 1
    assert "ValueError" in str(caught.value)


def test_an_exhausted_timeout_says_the_request_did_not_complete(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    import httpx

    client = FakeClient(models=FakeModels(outcomes=[httpx.ConnectTimeout("no route")]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    message = str(caught.value)
    assert "did not complete" in message
    assert f"{MAX_ATTEMPTS} attempts" in message


def test_a_candidate_with_no_finish_reason_is_accepted(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """Absence is not a refusal; there is simply nothing to check."""
    client = client_returning(
        FakeResponse(text=answer(proposal()), candidates=[FakeCandidate(finish_reason=None)])
    )
    assert len(analyzer_for(client).find_candidates(chunk, context).candidates) == 1


def test_a_very_long_provider_message_is_truncated(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    """An unbounded echo is the shape a leak takes even without a credential."""
    from google.genai import errors

    noisy = errors.ClientError(
        400, {"error": {"message": "detalle " * 200, "status": "INVALID_ARGUMENT"}}
    )
    client = FakeClient(models=FakeModels(outcomes=[noisy]))
    analyzer = analyzer_for(client)
    with pytest.raises(AnalysisError) as caught:
        analyzer.find_candidates(chunk, context)
    assert "..." in str(caught.value)
    assert len(str(caught.value)) < 600


def test_token_usage_is_counted_so_a_run_can_report_what_it_spent(
    chunk: TranscriptChunk, context: AnalysisContext
) -> None:
    client = client_returning(FakeResponse(text=answer()))
    analyzer = analyzer_for(client)
    analyzer.find_candidates(chunk, context)
    assert analyzer.calls == 1
    assert analyzer.prompt_tokens == 1200
    assert analyzer.response_tokens == 300
