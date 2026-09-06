"""`analyze` with a real provider selected, driven against a stand-in analyzer.

The command grew a second mode in this pull request: with `--fixture` it
replays, and without one it builds whatever `analysis.provider` names. These
tests are about the seam between the two, which is where the interesting
mistakes live — a fixture run recorded as though Gemini had been called, a
credential read on a path that does not need one, a set of artifacts produced by
one analyzer being reused by the other.

No test here reaches the network. Where a provider is needed the CLI's factory
is replaced with one that returns a recording stand-in, and where the point is
that the factory itself refuses, nothing is replaced and the real refusal is
asserted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.analysis.prompt import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    select_prompt,
)
from content_engine.domain.analysis_rules import AnalyzerIdentity
from content_engine.domain.candidate_rules import PromptIdentity
from content_engine.domain.candidates import TranscriptChunk
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_ANALYSIS,
    EXIT_CONFIGURATION,
    EXIT_INVALID_INPUT,
    EXIT_SUCCESS,
    AnalysisError,
    ConfigurationError,
)
from content_engine.ports.analyzer import AnalysisContext, CandidateBatch
from content_engine.services.analysis_service import ARTIFACT_FILENAMES
from tests.conftest import Harness, cli_output, raw_candidate

runner = CliRunner()

MODEL = "gemini-3.5-flash-lite"
SECRET = "AIzaSy-not-a-real-key-0123456789"
CREDENTIAL = "GEMINI_API_KEY"


class RecordingAnalyzer:
    """Stands in for `GeminiContentAnalyzer`, naming itself as Gemini would.

    It reports the same identity the real adapter reports, which is the point:
    the manifest, the stage configuration and the fingerprint must all record a
    provider run, and a stand-in that named itself would prove nothing about
    what a real one leaves behind.
    """

    def __init__(self, fail: bool = False, model: str = MODEL) -> None:
        self.model = model
        self.fail = fail
        self.calls: list[str] = []

    @property
    def identity(self) -> AnalyzerIdentity:
        return AnalyzerIdentity(
            analyzer="gemini",
            analyzer_version="1.1",
            model=self.model,
            prompt=PromptIdentity(version=PROMPT_VERSION, sha256=PROMPT_SHA256),
            fixture_sha256=None,
        )

    def find_candidates(self, chunk: TranscriptChunk, context: AnalysisContext) -> CandidateBatch:
        self.calls.append(chunk.id)
        if self.fail:
            raise AnalysisError("Gemini refused the request for chunk_0000: 503 UNAVAILABLE")
        return CandidateBatch(
            chunk_id=chunk.id,
            candidates=(raw_candidate(10.2, 39.4), raw_candidate(60.0, 85.0, hook=70)),
            raw_response='{"candidates": [{"start": 10.2, "end": 39.4}]}',
            model=self.model,
        )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> RecordingAnalyzer:
    """Replace the CLI's factory, so no client and no credential are needed."""
    analyzer = RecordingAnalyzer()
    monkeypatch.setattr(cli, "build_gemini_analyzer", lambda settings, prompt: analyzer)
    return analyzer


def analyze(harness: Harness, *extra: str) -> Any:
    """Provider mode: no --fixture."""
    return runner.invoke(cli.app, ["analyze", harness.run_id, *extra])


def replay(harness: Harness, *extra: str) -> Any:
    return runner.invoke(
        cli.app, ["analyze", harness.run_id, "--fixture", str(harness.fixture_path), *extra]
    )


# --- the fixture path is unchanged and needs nothing -------------------------


def test_a_fixture_run_still_works_with_no_credential_present(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CREDENTIAL, raising=False)

    result = replay(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert harness.manifest()["status"] == RunStatus.ANALYZED


def test_a_fixture_run_never_consults_the_credential(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely "works without it": the variable is never looked at."""
    seen: list[str] = []
    real = os.getenv

    def watching(name: str, default: Any = None) -> Any:
        seen.append(name)
        return real(name, default)

    monkeypatch.setenv(CREDENTIAL, SECRET)
    monkeypatch.setattr(os, "getenv", watching)

    assert replay(harness).exit_code == EXIT_SUCCESS
    assert CREDENTIAL not in seen


def test_a_fixture_run_is_never_recorded_as_gemini(harness: Harness) -> None:
    replay(harness)

    versions = harness.manifest()["versions"]
    assert versions["analysis_provider"] == "fixture"
    assert versions["analysis_model"] != MODEL
    config = json.loads(
        harness.analysis.joinpath("config.effective.json").read_text(encoding="utf-8")
    )
    assert config["analyzer"] == "fixture"
    assert config["fixture_sha256"] is not None
    # What the configuration *wanted* is recorded beside what ran, so the two
    # are distinguishable rather than one being inferred from the other.
    assert config["provider_configured"] == "gemini"


# --- provider mode requires a credential, and refuses before touching anything


def test_provider_mode_without_a_credential_is_a_configuration_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CREDENTIAL, raising=False)

    result = analyze(harness)

    assert result.exit_code == EXIT_CONFIGURATION
    assert CREDENTIAL in cli_output(result)


def test_a_refused_configuration_leaves_the_run_exactly_as_it_was(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 is "this machine is not set up", not "the analysis failed"."""
    monkeypatch.delenv(CREDENTIAL, raising=False)
    before = harness.manifest()

    analyze(harness)

    assert harness.manifest() == before
    assert harness.manifest()["status"] == RunStatus.TRANSCRIBED
    assert not list(harness.analysis.glob("*.json"))


def test_a_missing_credential_shows_no_traceback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CREDENTIAL, raising=False)

    result = analyze(harness)

    assert "Traceback" not in cli_output(result)
    assert "--fixture" in cli_output(result)


# --- a successful provider run ----------------------------------------------


def test_provider_mode_reaches_analyzed_and_calls_once_per_chunk(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    result = analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert harness.manifest()["status"] == RunStatus.ANALYZED
    assert provider.calls == ["chunk_0000"]
    for name in ARTIFACT_FILENAMES:
        assert harness.analysis.joinpath(name).is_file(), name


def test_the_manifest_records_the_real_provider_model_and_prompt(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    analyze(harness)

    versions = harness.manifest()["versions"]
    assert versions["analysis_provider"] == "gemini"
    assert versions["analysis_model"] == MODEL


def test_the_stage_configuration_records_the_real_prompt_and_no_fixture(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    analyze(harness)

    config = json.loads(
        harness.analysis.joinpath("config.effective.json").read_text(encoding="utf-8")
    )
    assert config["analyzer"] == "gemini"
    assert config["model"] == MODEL
    assert config["prompt_version"] == PROMPT_VERSION
    assert config["prompt_sha256"] == PROMPT_SHA256
    assert config["fixture_sha256"] is None


# --- a failing provider run --------------------------------------------------


def test_a_provider_failure_ends_the_run_as_failed_analysis(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "build_gemini_analyzer", lambda settings, prompt: RecordingAnalyzer(fail=True)
    )

    result = analyze(harness)

    assert result.exit_code == EXIT_ANALYSIS
    assert harness.manifest()["status"] == RunStatus.FAILED_ANALYSIS


def test_a_provider_failure_writes_no_artifact(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "build_gemini_analyzer", lambda settings, prompt: RecordingAnalyzer(fail=True)
    )

    analyze(harness)

    assert not list(harness.analysis.glob("*.json"))
    assert not list(harness.run_path.rglob("*.tmp"))


# --- reuse -------------------------------------------------------------------


def test_a_second_provider_run_reuses_without_calling_the_provider(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """The whole point of reuse: an identical invocation costs nothing."""
    analyze(harness)
    before = harness.snapshot()
    provider.calls.clear()

    result = analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert "reused" in cli_output(result)
    assert provider.calls == []
    assert harness.snapshot() == before


def test_force_calls_the_provider_again(harness: Harness, provider: RecordingAnalyzer) -> None:
    analyze(harness)
    provider.calls.clear()

    result = analyze(harness, "--force")

    assert result.exit_code == EXIT_SUCCESS
    assert provider.calls == ["chunk_0000"]


# --- the two modes do not share artifacts ------------------------------------


def test_artifacts_produced_by_a_fixture_are_not_reused_by_the_provider(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """Different analyzer, different stage configuration, so reuse is refused.

    Nothing special-cases this. The stage configuration names the analyzer that
    ran, its digest is what reuse compares, and a fixture and Gemini can never
    produce the same one.
    """
    assert replay(harness).exit_code == EXIT_SUCCESS
    before = harness.snapshot()

    result = analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert provider.calls == []
    assert harness.snapshot() == before


def test_artifacts_produced_by_the_provider_are_not_reused_by_a_fixture(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    assert analyze(harness).exit_code == EXIT_SUCCESS
    before = harness.snapshot()

    result = replay(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert harness.snapshot() == before


def test_switching_mode_with_force_replaces_the_artifacts(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    replay(harness)

    assert analyze(harness, "--force").exit_code == EXIT_SUCCESS

    assert harness.manifest()["versions"]["analysis_provider"] == "gemini"
    record = harness.manifest()["stages"][RunStage.ANALYSIS.value]
    assert len(record["fingerprint"]) == 64


# --- secrets -----------------------------------------------------------------


def test_no_credential_reaches_any_file_in_the_workspace(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Searched recursively, bytes rather than parsed, so nothing is missed."""
    monkeypatch.setenv(CREDENTIAL, SECRET)

    assert analyze(harness).exit_code == EXIT_SUCCESS

    needle = SECRET.encode("utf-8")
    inspected = 0
    for path in harness.run_path.rglob("*"):
        if path.is_file():
            inspected += 1
            assert needle not in path.read_bytes(), path
            assert b"GEMINI_API_KEY" not in path.read_bytes(), path
    assert inspected >= len(ARTIFACT_FILENAMES)


def test_the_credential_name_does_not_appear_in_a_successful_report(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CREDENTIAL, SECRET)

    result = analyze(harness)

    assert SECRET not in cli_output(result)


# --- the command surface -----------------------------------------------------


def test_the_fixture_option_is_optional_and_documented() -> None:
    result = runner.invoke(cli.app, ["analyze", "--help"])

    assert result.exit_code == EXIT_SUCCESS
    output = cli_output(result)
    assert "--fixture" in output
    assert "--force" in output


def test_an_unknown_run_is_refused_before_any_provider_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing is constructed for a run that does not exist."""

    def explode(settings: Any, prompt: Any) -> Any:
        raise AssertionError("a provider was built for a run that does not exist")

    monkeypatch.setattr(cli, "build_gemini_analyzer", explode)

    result = runner.invoke(cli.app, ["analyze", "20260101T000000-nope-abcdef"])

    assert result.exit_code == EXIT_INVALID_INPUT


def test_a_provider_with_no_adapter_is_refused_by_name(settings: Any) -> None:
    """Defensive: unreachable while AnalysisProvider has a single member.

    Written as its own branch rather than as an `else` on the Gemini check so
    that adding a provider to the enum without an adapter fails here, naming
    the provider, instead of silently being treated as Gemini and calling the
    wrong API with the wrong prompt.
    """
    settings.analysis.provider = "openai"
    prompt = select_prompt("v1")

    with pytest.raises(ConfigurationError) as caught:
        cli._expected_identity(settings, None, prompt)

    assert "openai" in str(caught.value)
    assert "--fixture" in str(caught.value)
