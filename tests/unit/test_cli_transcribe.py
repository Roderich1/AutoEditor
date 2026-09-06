"""The transcribe command: reuse, refusal and forced regeneration."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from content_engine import cli
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import WORKSPACE_ENV_VAR, load_settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    EXIT_INVALID_INPUT,
    EXIT_TRANSCRIPTION,
    TranscriptionProviderError,
)
from content_engine.domain.models import TRANSCRIPT_SCHEMA_VERSION, RawTranscription
from content_engine.services.run_service import RunService
from tests.conftest import FakeTranscriber, cli_output, fake_process, raw_segment, raw_transcription

runner = CliRunner()
AUDIO_DURATION = 10.0


@dataclass
class Harness:
    run_id: str
    run_path: Path
    transcriber: FakeTranscriber


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("workspace")))
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

    transcriber = FakeTranscriber(
        raw_transcription((raw_segment(0.0, 1.0, "hola"),), AUDIO_DURATION)
    )
    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: transcriber)
    return Harness(run_path.name, run_path, transcriber)


def _transcribe(harness: Harness, *extra: str) -> Any:
    return runner.invoke(cli.app, ["transcribe", harness.run_id, *extra])


def _manifest(harness: Harness) -> dict[str, Any]:
    return json.loads(harness.run_path.joinpath("manifest.json").read_text(encoding="utf-8"))


def test_a_first_transcription_records_state_and_fingerprint(harness: Harness) -> None:
    result = _transcribe(harness)

    assert result.exit_code == 0
    assert "Transcript ready" in cli_output(result)
    manifest = _manifest(harness)
    assert manifest["status"] == RunStatus.TRANSCRIBED.value
    record = manifest["stages"][RunStage.TRANSCRIPTION.value]
    assert record["schema_version"] == TRANSCRIPT_SCHEMA_VERSION
    assert len(record["fingerprint"]) == 64
    assert harness.run_path.joinpath("transcript", "metrics.json").is_file()


def test_an_unchanged_run_is_reused_without_calling_the_provider(harness: Harness) -> None:
    _transcribe(harness)
    result = _transcribe(harness)

    assert result.exit_code == 0
    assert "Transcript reused" in cli_output(result)
    assert len(harness.transcriber.calls) == 1


def test_changed_inputs_are_refused_rather_than_silently_reused(
    harness: Harness, tmp_path: Path
) -> None:
    _transcribe(harness)
    profile = tmp_path.joinpath("changed.toml")
    profile.write_text("[transcription]\nbeam_size = 1\n", encoding="utf-8")

    result = _transcribe(harness, "--config", str(profile))

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Incompatible artifact" in cli_output(result)
    assert "--force" in cli_output(result)
    assert len(harness.transcriber.calls) == 1


def test_a_transcript_without_a_recorded_fingerprint_is_refused(harness: Harness) -> None:
    _transcribe(harness)
    manifest_path = harness.run_path.joinpath("manifest.json")
    payload = _manifest(harness)
    payload["stages"] = {}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = _transcribe(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "no fingerprint was recorded" in cli_output(result)


def test_a_transcript_from_another_schema_is_refused(harness: Harness) -> None:
    _transcribe(harness)
    manifest_path = harness.run_path.joinpath("manifest.json")
    payload = _manifest(harness)
    payload["stages"][RunStage.TRANSCRIPTION.value]["schema_version"] = 99
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = _transcribe(harness)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "schema 99" in cli_output(result)


def test_force_regenerates_and_updates_the_fingerprint(harness: Harness, tmp_path: Path) -> None:
    _transcribe(harness)
    original = _manifest(harness)["stages"][RunStage.TRANSCRIPTION.value]["fingerprint"]
    profile = tmp_path.joinpath("changed.toml")
    profile.write_text("[transcription]\nbeam_size = 1\n", encoding="utf-8")

    result = _transcribe(harness, "--config", str(profile), "--force")

    assert result.exit_code == 0
    assert len(harness.transcriber.calls) == 2
    assert _manifest(harness)["stages"][RunStage.TRANSCRIPTION.value]["fingerprint"] != original


def test_force_warns_that_downstream_artifacts_became_stale(harness: Harness) -> None:
    _transcribe(harness)
    harness.run_path.joinpath("analysis", "candidates.json").write_text("[]", encoding="utf-8")

    result = _transcribe(harness, "--force")

    assert result.exit_code == 0
    assert "stale" in cli_output(result)
    assert "CE-052" in cli_output(result)


def test_a_provider_failure_is_recorded_against_the_transcription_stage(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing(FakeTranscriber):
        def transcribe(self, audio_path: Path, options: Any, hardware: Any) -> RawTranscription:
            raise TranscriptionProviderError("CUDA out of memory")

    monkeypatch.setattr(
        cli,
        "FasterWhisperTranscriber",
        lambda: Failing(raw_transcription(())),
    )

    result = _transcribe(harness)

    assert result.exit_code == EXIT_TRANSCRIPTION
    assert "kept for diagnosis" in cli_output(result)
    manifest = _manifest(harness)
    assert manifest["status"] == RunStatus.FAILED_TRANSCRIPTION.value
    assert manifest["failure"]["stage"] == RunStage.TRANSCRIPTION.value
    assert manifest["failure"]["error_type"] == "TranscriptionProviderError"


def test_output_that_disagrees_with_the_audio_fails_the_transcription_stage(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "FasterWhisperTranscriber",
        lambda: FakeTranscriber(raw_transcription((raw_segment(0.0, 400.0, "demasiado"),), 400.0)),
    )

    result = _transcribe(harness)

    assert result.exit_code == EXIT_TRANSCRIPTION
    assert "not trusted" in cli_output(result)
    assert _manifest(harness)["status"] == RunStatus.FAILED_TRANSCRIPTION.value


def test_an_empty_transcript_warns_but_still_completes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "FasterWhisperTranscriber",
        lambda: FakeTranscriber(raw_transcription((), AUDIO_DURATION)),
    )

    result = _transcribe(harness)

    assert result.exit_code == 0
    assert "no segments" in cli_output(result)
    assert _manifest(harness)["status"] == RunStatus.TRANSCRIBED.value


def test_a_retry_after_failure_reaches_transcribed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "FasterWhisperTranscriber",
        lambda: FakeTranscriber(raw_transcription((raw_segment(0.0, 400.0, "malo"),), 400.0)),
    )
    _transcribe(harness)
    assert _manifest(harness)["status"] == RunStatus.FAILED_TRANSCRIPTION.value

    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: harness.transcriber)
    result = _transcribe(harness)

    assert result.exit_code == 0
    assert _manifest(harness)["status"] == RunStatus.TRANSCRIBED.value


def test_the_manifest_names_the_model_that_actually_produced_the_transcript(
    harness: Harness, tmp_path: Path
) -> None:
    """A run created under one model and transcribed under another must not lie.

    manifest.versions.transcription_model is a claim about the artifact on disk.
    Recording the model configured at ``run`` time while a different one did the
    work would file the experiment under settings that never ran.
    """
    profile = tmp_path.joinpath("other-model.toml")
    profile.write_text('[transcription]\nmodel = "tiny"\n', encoding="utf-8")

    result = _transcribe(harness, "--config", str(profile))

    assert result.exit_code == 0
    manifest = _manifest(harness)
    transcript = json.loads(
        harness.run_path.joinpath("transcript", "transcript.json").read_text(encoding="utf-8")
    )
    assert manifest["versions"]["transcription_model"] == transcript["model"]


def test_a_configuration_that_differs_from_the_run_is_reported(
    harness: Harness, tmp_path: Path
) -> None:
    profile = tmp_path.joinpath("other-model.toml")
    profile.write_text('[transcription]\nmodel = "tiny"\n', encoding="utf-8")

    result = _transcribe(harness, "--config", str(profile))

    assert result.exit_code == 0
    assert "not the one" in cli_output(result)
    assert _manifest(harness)["config_sha256"] not in cli_output(result)


def test_the_unchanged_configuration_is_not_reported_as_drift(harness: Harness) -> None:
    result = _transcribe(harness)

    assert result.exit_code == 0
    assert "not the one" not in cli_output(result)
