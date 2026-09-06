"""Three defects an independent review of PR #6 found, pinned as tests.

**The manifest never learned the prompt.** `manifest.versions.prompt_version`
and `prompt_sha256` stayed null after a real provider run, so the one place a
reader looks to ask "which prompt produced this run" answered "none" for every
run ever made. Worse in the other direction: switching executor with `--force`
has to clear them, or a Gemini run followed by a fixture run would leave the
fixture's artifacts described by Gemini's prompt.

**Reuse required a credential.** The analyzer was constructed before the reuse
decision, so verifying four finished artifacts demanded an API key and an SDK
client. Nothing about reading four files back needs either. A configuration
that cannot *produce* must still be able to *verify*.

**`analysis.prompt_version` was ignored.** Covered in test_prompt_selection;
what is asserted here is the consequence at the command level — a profile that
names another prompt must not silently reuse artifacts produced by this one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.analysis.prompt import PROMPT_SHA256, PROMPT_VERSION
from content_engine.domain.enums import RunStatus
from content_engine.domain.exceptions import (
    EXIT_CONFIGURATION,
    EXIT_INVALID_INPUT,
    EXIT_SUCCESS,
)
from content_engine.services.analysis_service import ARTIFACT_FILENAMES
from tests.conftest import Harness, cli_output
from tests.unit.test_cli_analyze_provider import MODEL, RecordingAnalyzer

runner = CliRunner()

CREDENTIAL = "GEMINI_API_KEY"
SECRET = "AIzaSy-not-a-real-key-0123456789"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> RecordingAnalyzer:
    """A stand-in for the real adapter, installed where the CLI builds one."""
    analyzer = RecordingAnalyzer()
    monkeypatch.setattr(cli, "build_gemini_analyzer", lambda settings, prompt: analyzer)
    return analyzer


def analyze(harness: Harness, *extra: str) -> Any:
    return runner.invoke(cli.app, ["analyze", harness.run_id, *extra])


def replay(harness: Harness, *extra: str) -> Any:
    return runner.invoke(
        cli.app, ["analyze", harness.run_id, "--fixture", str(harness.fixture_path), *extra]
    )


def versions(harness: Harness) -> dict[str, Any]:
    return harness.manifest()["versions"]


# --- A. the manifest records which prompt produced the run -------------------


def test_a_provider_run_records_the_real_prompt_in_the_manifest(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    assert analyze(harness).exit_code == EXIT_SUCCESS

    assert versions(harness)["prompt_version"] == PROMPT_VERSION
    assert versions(harness)["prompt_sha256"] == PROMPT_SHA256


def test_a_fixture_run_leaves_the_prompt_fields_null(harness: Harness) -> None:
    """A fixture used no packaged prompt, so it must claim none.

    The fixture's own `fake-fixture/v1` belongs to the stage configuration,
    where it is a truthful record of the stand-in. These manifest fields mean
    something else — the versioned prompt resource that was really sent — and a
    fixture sent nothing.
    """
    assert replay(harness).exit_code == EXIT_SUCCESS

    assert versions(harness)["prompt_version"] is None
    assert versions(harness)["prompt_sha256"] is None
    config = json.loads(
        harness.analysis.joinpath("config.effective.json").read_text(encoding="utf-8")
    )
    assert config["prompt_version"] == "fake-fixture/v1"


def test_forcing_from_fixture_to_provider_fills_the_prompt_fields(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    assert replay(harness).exit_code == EXIT_SUCCESS
    assert versions(harness)["prompt_version"] is None

    assert analyze(harness, "--force").exit_code == EXIT_SUCCESS

    assert versions(harness)["prompt_version"] == PROMPT_VERSION
    assert versions(harness)["prompt_sha256"] == PROMPT_SHA256
    assert versions(harness)["analysis_provider"] == "gemini"


def test_forcing_from_provider_to_fixture_clears_the_prompt_fields(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """The dangerous direction: a stale value would describe the wrong run."""
    assert analyze(harness).exit_code == EXIT_SUCCESS
    assert versions(harness)["prompt_version"] == PROMPT_VERSION

    assert replay(harness, "--force").exit_code == EXIT_SUCCESS

    assert versions(harness)["prompt_version"] is None
    assert versions(harness)["prompt_sha256"] is None
    assert versions(harness)["analysis_provider"] == "fixture"


# --- B. reuse verifies artifacts, and needs no credential --------------------


def test_reuse_of_a_provider_run_works_with_no_credential(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CREDENTIAL, SECRET)
    assert analyze(harness).exit_code == EXIT_SUCCESS
    before = harness.snapshot()
    provider.calls.clear()

    # The key goes away. Four finished artifacts do not stop being verifiable.
    monkeypatch.delenv(CREDENTIAL, raising=False)
    result = analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert "reused" in cli_output(result)
    assert provider.calls == []
    assert harness.snapshot() == before


def test_reuse_builds_no_client_and_reads_no_credential(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely "it worked": neither the factory nor the variable is touched."""
    assert analyze(harness).exit_code == EXIT_SUCCESS

    def explode(settings: Any, prompt: Any) -> Any:
        raise AssertionError("reuse built a provider client")

    seen: list[str] = []
    real = os.getenv

    def watching(name: str, default: Any = None) -> Any:
        seen.append(name)
        return real(name, default)

    monkeypatch.setattr(cli, "build_gemini_analyzer", explode)
    monkeypatch.setenv(CREDENTIAL, SECRET)
    monkeypatch.setattr(os, "getenv", watching)

    assert analyze(harness).exit_code == EXIT_SUCCESS
    assert CREDENTIAL not in seen


def test_a_run_left_failed_recovers_without_a_credential(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery is a verification too, so it must not need a key either."""
    assert analyze(harness).exit_code == EXIT_SUCCESS
    manifest_path = harness.run_path.joinpath("manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["status"] = RunStatus.FAILED_ANALYSIS.value
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    monkeypatch.delenv(CREDENTIAL, raising=False)
    result = analyze(harness)

    assert result.exit_code == EXIT_SUCCESS
    assert "recovered" in cli_output(result).lower()
    assert harness.manifest()["status"] == RunStatus.ANALYZED


def test_producing_without_a_credential_still_refuses(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the check later must not remove it."""
    monkeypatch.delenv(CREDENTIAL, raising=False)

    result = analyze(harness)

    assert result.exit_code == EXIT_CONFIGURATION
    assert CREDENTIAL in cli_output(result)
    assert harness.manifest()["status"] == RunStatus.TRANSCRIBED
    assert not list(harness.analysis.glob("*.json"))


def test_forcing_without_a_credential_refuses_and_keeps_the_artifacts(
    harness: Harness, provider: RecordingAnalyzer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--force is a production, so it needs the key; and it must destroy nothing."""
    assert analyze(harness).exit_code == EXIT_SUCCESS
    before = harness.snapshot()

    monkeypatch.setattr(cli, "build_gemini_analyzer", cli.build_gemini_analyzer)
    monkeypatch.delenv(CREDENTIAL, raising=False)
    result = analyze(harness, "--force")

    assert result.exit_code == EXIT_CONFIGURATION
    assert harness.snapshot() == before


def test_a_missing_credential_never_marks_the_run_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was called, so FAILED_ANALYSIS would assert a failure that never happened."""
    monkeypatch.delenv(CREDENTIAL, raising=False)

    analyze(harness)

    assert harness.manifest()["status"] != RunStatus.FAILED_ANALYSIS
    assert harness.manifest()["failure"] is None


# --- C. a different configured prompt is a different experiment --------------


def profile(tmp_path: Path, version: str) -> Path:
    path = tmp_path.joinpath(f"prompt-{version}.toml")
    path.write_text(f'[analysis]\nprompt_version = "{version}"\n', encoding="utf-8", newline="\n")
    return path


def test_an_unknown_configured_prompt_is_refused_before_the_run_is_touched(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    path = profile(harness.tmp_path, "v2")

    result = analyze(harness, "--config", str(path))

    assert result.exit_code == EXIT_CONFIGURATION
    assert "prompt_version" in cli_output(result)
    assert provider.calls == []
    assert harness.manifest()["status"] == RunStatus.TRANSCRIBED
    assert not list(harness.analysis.glob("*.json"))


def test_an_unknown_configured_prompt_is_refused_even_with_a_fixture(harness: Harness) -> None:
    """A configuration naming a prompt that does not exist is broken either way.

    The fixture would not have sent it, but refusing only in provider mode would
    let a profile carry a typo indefinitely and surface it on the one run that
    costs money.
    """
    path = profile(harness.tmp_path, "v2")

    result = replay(harness, "--config", str(path))

    assert result.exit_code == EXIT_CONFIGURATION
    assert not list(harness.analysis.glob("*.json"))


def test_an_unknown_configured_prompt_does_not_silently_reuse(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """The failure this replaces: artifacts from v1 reused for a run asking v2."""
    assert analyze(harness).exit_code == EXIT_SUCCESS
    before = harness.snapshot()
    path = profile(harness.tmp_path, "v2")

    result = analyze(harness, "--config", str(path))

    assert result.exit_code == EXIT_CONFIGURATION
    assert "reused" not in cli_output(result)
    assert harness.snapshot() == before


def test_the_known_prompt_version_still_analyses(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """The refusal must be about the unknown name, not about --config at all."""
    path = profile(harness.tmp_path, "v1")

    result = analyze(harness, "--config", str(path))

    assert result.exit_code == EXIT_SUCCESS
    assert versions(harness)["prompt_version"] == PROMPT_VERSION


def test_the_stage_configuration_records_the_selected_prompt(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """Which is what makes a prompt change invalidate reuse rather than hide."""
    assert analyze(harness).exit_code == EXIT_SUCCESS

    config = json.loads(
        harness.analysis.joinpath("config.effective.json").read_text(encoding="utf-8")
    )
    assert config["prompt_version"] == PROMPT_VERSION
    assert config["prompt_sha256"] == PROMPT_SHA256
    assert config["model"] == MODEL


# --- the four artifacts are still protected ----------------------------------


def test_a_provider_run_whose_artifacts_were_edited_is_still_refused(
    harness: Harness, provider: RecordingAnalyzer
) -> None:
    """Reuse got cheaper to reach; it must not have got weaker."""
    assert analyze(harness).exit_code == EXIT_SUCCESS
    target = harness.analysis.joinpath(ARTIFACT_FILENAMES[-1])
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["candidates"][0]["topic"] = "otro tema"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    result = analyze(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
