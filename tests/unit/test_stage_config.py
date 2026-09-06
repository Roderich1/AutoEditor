"""The effective configuration of the transcription stage.

A run carries two configurations, deliberately. ``config.effective.json`` at the
run root is the configuration the experiment was *created* with;
``transcript/config.effective.json`` is what the transcription stage actually
ran, with ``auto`` already resolved to a real device. They differ whenever
``transcribe --config`` names another profile, and both must be recoverable
afterwards without reversing an opaque digest.
"""

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
from content_engine.domain.models import (
    TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION,
    ResolvedHardware,
    TranscriptionStageConfig,
)
from content_engine.domain.transcript_rules import (
    NORMALIZATION_RULES_VERSION,
    stage_config,
    stage_config_sha256,
    transcription_fingerprint,
)
from content_engine.services.run_service import RunService
from content_engine.services.transcription_service import STAGE_CONFIG_FILENAME
from content_engine.utils.hashing import sha256_file
from tests.conftest import FakeTranscriber, fake_process, raw_segment, raw_transcription

runner = CliRunner()
AUDIO_DURATION = 10.0
HARDWARE = ResolvedHardware(device="cpu", compute_type="int8")

#: The profiles a developer actually reaches for, written out here rather than
#: read from configs/ so the test states the expectation instead of echoing it.
FAST = '[transcription]\nmodel = "small"\ndevice = "cpu"\ncompute_type = "int8"\nbeam_size = 5\n'
DEFAULT_MODEL = "large-v3"
FAST_MODEL = "small"


@dataclass
class Harness:
    run_id: str
    run_path: Path
    transcriber: FakeTranscriber
    profiles: Path


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
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

    profiles = tmp_path.joinpath("profiles")
    profiles.mkdir()
    profiles.joinpath("fast.toml").write_text(FAST, encoding="utf-8")

    transcriber = FakeTranscriber(
        raw_transcription((raw_segment(0.0, 1.0, "hola"),), AUDIO_DURATION),
        hardware=HARDWARE,
    )
    monkeypatch.setattr(cli, "FasterWhisperTranscriber", lambda: transcriber)

    video = tmp_path.joinpath("sample.mp4")
    video.write_bytes(b"video")
    return Harness("", video, transcriber, profiles)  # replaced by _create_run


def _create_run(harness: Harness, tmp_path: Path, profile: str | None = None) -> Harness:
    """Create a run under the default configuration or under a profile."""
    settings = load_settings(harness.profiles.joinpath(profile) if profile else None)
    workspace = RunWorkspace(settings.workspace.root)
    service = RunService(settings, workspace)
    run_path, manifest = service.create(harness.run_path)
    manifest = service.advance(run_path, manifest, RunStatus.INSPECTED)
    run_path.joinpath("audio", "source.wav").write_bytes(b"wav-bytes")
    service.advance(run_path, manifest, RunStatus.AUDIO_READY)
    return Harness(run_path.name, run_path, harness.transcriber, harness.profiles)


def _transcribe(harness: Harness, profile: str | None = None, *extra: str) -> Any:
    arguments = ["transcribe", harness.run_id]
    if profile is not None:
        arguments += ["--config", str(harness.profiles.joinpath(profile))]
    return runner.invoke(cli.app, [*arguments, *extra])


def _stage_config_path(harness: Harness) -> Path:
    return harness.run_path.joinpath("transcript", STAGE_CONFIG_FILENAME)


def _stage_config(harness: Harness) -> TranscriptionStageConfig:
    return TranscriptionStageConfig.model_validate(
        json.loads(_stage_config_path(harness).read_text(encoding="utf-8"))
    )


def _read(harness: Harness, *parts: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        harness.run_path.joinpath(*parts).read_text(encoding="utf-8")
    )
    return payload


def _assert_coherent(harness: Harness, expected_model: str) -> None:
    """The five records of one stage must agree, down to the fingerprint."""
    stage = _stage_config(harness)
    metrics = _read(harness, "transcript", "metrics.json")
    transcript = _read(harness, "transcript", "transcript.json")
    manifest = _read(harness, "manifest.json")
    record = manifest["stages"][RunStage.TRANSCRIPTION.value]

    assert stage.model == expected_model
    assert stage.model == metrics["model"] == transcript["model"]
    assert stage.model == manifest["versions"]["transcription_model"]
    assert stage.device_requested == metrics["device_requested"]
    assert stage.device_resolved == metrics["device_resolved"] == HARDWARE.device
    assert stage.compute_type_requested == metrics["compute_type_requested"]
    assert stage.compute_type_resolved == metrics["compute_type_resolved"]
    assert stage.normalization_version == metrics["normalization"]["rules_version"]
    assert stage.schema_version == TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION
    assert stage.normalization_version == NORMALIZATION_RULES_VERSION

    # The manifest hash addresses this exact artifact.
    assert record["stage_config_sha256"] == stage_config_sha256(stage)

    # And the readable artifact reconstructs the opaque fingerprint, which is
    # what makes the two representations one record rather than two.
    audio_sha256 = sha256_file(harness.run_path.joinpath("audio", "source.wav"))
    options = harness.transcriber.calls[-1]
    assert options.model == stage.model
    assert record["fingerprint"] == transcription_fingerprint(audio_sha256, stage)
    assert record["fingerprint"] == transcription_fingerprint(
        audio_sha256, stage_config(options, HARDWARE)
    )


def test_run_default_then_transcribe_default(harness: Harness, tmp_path: Path) -> None:
    harness = _create_run(harness, tmp_path)

    result = _transcribe(harness)

    assert result.exit_code == 0
    assert "not the one" not in result.output
    _assert_coherent(harness, DEFAULT_MODEL)


def test_run_default_then_transcribe_fast(harness: Harness, tmp_path: Path) -> None:
    """The case that used to leave the run describing a configuration never run."""
    harness = _create_run(harness, tmp_path)

    result = _transcribe(harness, "fast.toml")

    assert result.exit_code == 0
    assert "not the one" in result.output
    _assert_coherent(harness, FAST_MODEL)

    # The run-level configuration still describes how the run was created.
    assert _read(harness, "config.effective.json")["transcription"]["model"] == DEFAULT_MODEL


def test_run_fast_then_transcribe_fast(harness: Harness, tmp_path: Path) -> None:
    harness = _create_run(harness, tmp_path, "fast.toml")

    result = _transcribe(harness, "fast.toml")

    assert result.exit_code == 0
    assert "not the one" not in result.output
    _assert_coherent(harness, FAST_MODEL)
    assert _read(harness, "config.effective.json")["transcription"]["model"] == FAST_MODEL


def test_reuse_leaves_the_stage_configuration_untouched(harness: Harness, tmp_path: Path) -> None:
    harness = _create_run(harness, tmp_path)
    _transcribe(harness, "fast.toml")
    before = _stage_config_path(harness).read_bytes()

    result = _transcribe(harness, "fast.toml")

    assert result.exit_code == 0
    assert "Transcript reused" in result.output
    assert len(harness.transcriber.calls) == 1
    assert _stage_config_path(harness).read_bytes() == before


def test_an_incompatible_configuration_leaves_the_stage_configuration_untouched(
    harness: Harness, tmp_path: Path
) -> None:
    harness = _create_run(harness, tmp_path)
    _transcribe(harness, "fast.toml")
    before = _stage_config_path(harness).read_bytes()
    harness.profiles.joinpath("other.toml").write_text(
        FAST.replace("beam_size = 5", "beam_size = 1"), encoding="utf-8"
    )

    result = _transcribe(harness, "other.toml")

    assert result.exit_code != 0
    assert "Incompatible artifact" in result.output
    assert _stage_config_path(harness).read_bytes() == before


def test_force_rewrites_the_stage_configuration(harness: Harness, tmp_path: Path) -> None:
    harness = _create_run(harness, tmp_path)
    _transcribe(harness)
    assert _stage_config(harness).model == DEFAULT_MODEL
    first = _read(harness, "manifest.json")["stages"][RunStage.TRANSCRIPTION.value]

    result = _transcribe(harness, "fast.toml", "--force")

    assert result.exit_code == 0
    _assert_coherent(harness, FAST_MODEL)
    second = _read(harness, "manifest.json")["stages"][RunStage.TRANSCRIPTION.value]
    assert second["stage_config_sha256"] != first["stage_config_sha256"]
    assert second["fingerprint"] != first["fingerprint"]


def test_a_stage_that_never_completed_leaves_no_effective_configuration(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused transcript must not leave a configuration claiming it ran."""
    harness = _create_run(harness, tmp_path)
    monkeypatch.setattr(
        cli,
        "FasterWhisperTranscriber",
        lambda: FakeTranscriber(
            raw_transcription((raw_segment(0.0, 400.0, "demasiado"),), 400.0),
            hardware=HARDWARE,
        ),
    )

    result = _transcribe(harness)

    assert result.exit_code != 0
    assert not _stage_config_path(harness).exists()
    manifest = _read(harness, "manifest.json")
    assert manifest["status"] == RunStatus.FAILED_TRANSCRIPTION.value
    assert manifest["stages"] == {}


def test_the_stage_configuration_holds_no_workspace_path_or_credential(
    harness: Harness, tmp_path: Path
) -> None:
    harness = _create_run(harness, tmp_path)
    _transcribe(harness)

    text = _stage_config_path(harness).read_text(encoding="utf-8")

    assert str(tmp_path) not in text
    assert "workspace" not in text.lower()
    assert "key" not in text.lower()


def test_the_stage_configuration_is_utf8_lf_without_a_bom(harness: Harness, tmp_path: Path) -> None:
    harness = _create_run(harness, tmp_path)
    _transcribe(harness)

    data = _stage_config_path(harness).read_bytes()

    assert b"\r\n" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8").endswith("\n")


def test_the_stage_configuration_hash_ignores_layout_but_not_content() -> None:
    """The hash must survive reformatting and change with any real difference."""
    base = TranscriptionStageConfig(
        provider="faster-whisper",
        model="small",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        device_requested="auto",
        device_resolved="cpu",
        compute_type_requested="auto",
        compute_type_resolved="int8",
        normalization_version=NORMALIZATION_RULES_VERSION,
    )
    reordered = TranscriptionStageConfig.model_validate(
        dict(reversed(list(base.model_dump(mode="json").items())))
    )

    assert stage_config_sha256(reordered) == stage_config_sha256(base)
    assert stage_config_sha256(base.model_copy(update={"model": "tiny"})) != stage_config_sha256(
        base
    )
    assert stage_config_sha256(
        base.model_copy(update={"device_resolved": "cuda"})
    ) != stage_config_sha256(base)


# --------------------------------------------------------------------------
# Reuse verifies the artifact, not only the manifest.
#
# A digest in the manifest proves nothing about a file that is gone or was
# edited. Each case below tampers with exactly one thing and asserts that the
# transcript, the stage configuration and the manifest all survive untouched.
# --------------------------------------------------------------------------

EXIT_INVALID_INPUT = 3


@dataclass
class Snapshot:
    transcript: bytes
    stage_config: bytes
    manifest: bytes


def _snapshot(harness: Harness) -> Snapshot:
    return Snapshot(
        transcript=harness.run_path.joinpath("transcript", "transcript.json").read_bytes(),
        stage_config=_stage_config_path(harness).read_bytes(),
        manifest=harness.run_path.joinpath("manifest.json").read_bytes(),
    )


def _assert_untouched(harness: Harness, before: Snapshot) -> None:
    after = _snapshot(harness)

    assert after.transcript == before.transcript
    assert after.stage_config == before.stage_config
    assert after.manifest == before.manifest


@pytest.fixture
def transcribed(harness: Harness, tmp_path: Path) -> Harness:
    prepared = _create_run(harness, tmp_path)
    assert _transcribe(prepared).exit_code == 0
    return prepared


def test_a_valid_stage_configuration_is_reused(transcribed: Harness) -> None:
    before = _snapshot(transcribed)

    result = _transcribe(transcribed)

    assert result.exit_code == 0
    assert "Transcript reused" in result.output
    assert len(transcribed.transcriber.calls) == 1
    _assert_untouched(transcribed, before)


def test_a_missing_stage_configuration_refuses_reuse(transcribed: Harness) -> None:
    """The manifest still holds both digests; the file they describe is gone."""
    before = _snapshot(transcribed)
    _stage_config_path(transcribed).unlink()

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Incompatible artifact" in result.output
    assert "missing" in result.output
    assert len(transcribed.transcriber.calls) == 1
    assert transcribed.run_path.joinpath("manifest.json").read_bytes() == before.manifest


def test_a_corrupt_stage_configuration_refuses_reuse(transcribed: Harness) -> None:
    before = _snapshot(transcribed)
    _stage_config_path(transcribed).write_text('{"model": ', encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "Incompatible artifact" in result.output
    assert transcribed.run_path.joinpath("manifest.json").read_bytes() == before.manifest
    assert (
        transcribed.run_path.joinpath("transcript", "transcript.json").read_bytes()
        == before.transcript
    )


def test_a_stage_configuration_that_is_not_an_object_refuses_reuse(transcribed: Harness) -> None:
    _stage_config_path(transcribed).write_text("[1, 2, 3]", encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "does not contain a stage configuration object" in result.output


def test_an_unknown_stage_configuration_schema_refuses_reuse(transcribed: Harness) -> None:
    payload = json.loads(_stage_config_path(transcribed).read_text(encoding="utf-8"))
    payload["schema_version"] = TRANSCRIPTION_STAGE_CONFIG_SCHEMA_VERSION + 99
    _stage_config_path(transcribed).write_text(json.dumps(payload), encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "schema" in result.output


def test_a_stage_configuration_with_a_missing_field_refuses_reuse(transcribed: Harness) -> None:
    payload = json.loads(_stage_config_path(transcribed).read_text(encoding="utf-8"))
    del payload["beam_size"]
    _stage_config_path(transcribed).write_text(json.dumps(payload), encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "not a valid stage configuration" in result.output


def test_a_stage_configuration_that_no_longer_hashes_to_the_manifest_refuses_reuse(
    transcribed: Harness,
) -> None:
    before = _snapshot(transcribed)
    payload = json.loads(_stage_config_path(transcribed).read_text(encoding="utf-8"))
    payload["beam_size"] = 1
    _stage_config_path(transcribed).write_text(json.dumps(payload), encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "does not match the manifest" in result.output
    assert transcribed.run_path.joinpath("manifest.json").read_bytes() == before.manifest


def test_a_repaired_hash_does_not_rescue_an_edited_stage_configuration(
    transcribed: Harness,
) -> None:
    """The hardest case: edit the artifact and fix the manifest digest to match.

    The stage hash then agrees with itself, so only rebuilding the fingerprint
    from the audio and the artifact catches that the run no longer describes one
    execution.
    """
    stage_path = _stage_config_path(transcribed)
    payload = json.loads(stage_path.read_text(encoding="utf-8"))
    payload["beam_size"] = 1
    stage_path.write_text(json.dumps(payload), encoding="utf-8")

    edited = TranscriptionStageConfig.model_validate(payload)
    manifest_path = transcribed.run_path.joinpath("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"][RunStage.TRANSCRIPTION.value]["stage_config_sha256"] = stage_config_sha256(
        edited
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = _snapshot(transcribed)

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "cannot be rebuilt" in result.output
    _assert_untouched(transcribed, before)


def test_force_regenerates_after_the_stage_configuration_was_destroyed(
    transcribed: Harness,
) -> None:
    """--force is the documented way out of every refusal above."""
    _stage_config_path(transcribed).unlink()

    result = _transcribe(transcribed, None, "--force")

    assert result.exit_code == 0
    assert "Transcript ready" in result.output
    assert len(transcribed.transcriber.calls) == 2
    _assert_coherent(transcribed, DEFAULT_MODEL)


def test_force_regenerates_after_the_stage_configuration_was_corrupted(
    transcribed: Harness,
) -> None:
    _stage_config_path(transcribed).write_text("not json at all", encoding="utf-8")

    result = _transcribe(transcribed, None, "--force")

    assert result.exit_code == 0
    _assert_coherent(transcribed, DEFAULT_MODEL)


def test_a_transcript_without_a_stage_record_refuses_reuse(transcribed: Harness) -> None:
    manifest_path = transcribed.run_path.joinpath("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _transcribe(transcribed)

    assert result.exit_code == EXIT_INVALID_INPUT
    assert "no fingerprint was recorded" in result.output
