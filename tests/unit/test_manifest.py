from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.config import WORKSPACE_ENV_VAR, Settings, load_settings
from content_engine.domain.enums import RunStage, RunStatus
from content_engine.domain.exceptions import (
    CorruptArtifactError,
    InvalidRunStateError,
    UnsupportedSchemaVersionError,
)
from content_engine.domain.models import MANIFEST_SCHEMA_VERSION
from content_engine.services.run_service import RunService
from tests.conftest import fake_process


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("My Video.mp4")
    path.write_bytes(b"video-content")
    return path


@pytest.fixture
def run_service(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> RunService:
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )
    return RunService(settings, RunWorkspace(settings.workspace.root))


def test_manifest_matches_the_specified_shape(run_service: RunService, video: Path) -> None:
    run_path, manifest = run_service.create(video)
    payload = json.loads(run_path.joinpath("manifest.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert payload["status"] == RunStatus.CREATED
    assert payload["config_sha256"]
    assert set(payload["input"]) == {"path", "sha256", "size"}
    assert payload["stages"] == {}
    assert payload["failure"] is None
    assert manifest.run_id.startswith("20")
    assert "my-video" in manifest.run_id


def test_prompt_metadata_is_null_until_ce_026(run_service: RunService, video: Path) -> None:
    run_path, _ = run_service.create(video)
    versions = json.loads(run_path.joinpath("manifest.json").read_text(encoding="utf-8"))[
        "versions"
    ]

    assert versions["prompt_version"] is None
    assert versions["prompt_sha256"] is None
    assert versions["transcription_model"]
    assert versions["analysis_provider"]


def test_run_id_is_unique_per_execution(run_service: RunService, video: Path) -> None:
    """Two runs of the same source are two experiments, not one."""
    first = run_service.create(video)[1]
    second = run_service.create(video)[1]

    assert first.run_id != second.run_id
    assert first.config_sha256 == second.config_sha256
    assert first.input.sha256 == second.input.sha256


def test_effective_config_keeps_the_workspace_but_the_hash_does_not(
    run_service: RunService, video: Path
) -> None:
    run_path, manifest = run_service.create(video)
    effective = json.loads(run_path.joinpath("config.effective.json").read_text(encoding="utf-8"))

    assert effective["workspace"]["root"]
    assert manifest.config_sha256 not in json.dumps(effective)


def test_config_hash_is_identical_across_workspaces(
    tmp_path: Path, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "content_engine.services.run_service.run_command",
        lambda arguments, **_: fake_process(arguments, "ffmpeg version test\n"),
    )
    hashes = []
    for name in ("machine-a", "some/deeper/machine-b"):
        monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath(name)))
        settings = load_settings()
        service = RunService(settings, RunWorkspace(settings.workspace.root))
        hashes.append(service.create(video)[1].config_sha256)

    assert hashes[0] == hashes[1]


def test_artifacts_are_written_with_lf_and_no_bom(run_service: RunService, video: Path) -> None:
    run_path, _ = run_service.create(video)
    for name in ("manifest.json", "config.effective.json"):
        data = run_path.joinpath(name).read_bytes()
        assert b"\r\n" not in data
        assert not data.startswith(b"\xef\xbb\xbf")


def test_advance_records_the_new_state(run_service: RunService, video: Path) -> None:
    run_path, manifest = run_service.create(video)
    run_service.advance(run_path, manifest, RunStatus.INSPECTED)

    assert run_service.workspace.read_manifest(run_path).status == RunStatus.INSPECTED


def test_advance_refuses_an_impossible_transition(run_service: RunService, video: Path) -> None:
    run_path, manifest = run_service.create(video)

    with pytest.raises(InvalidRunStateError):
        run_service.advance(run_path, manifest, RunStatus.COMPLETED)


def test_failure_is_recorded_with_stage_and_cause(run_service: RunService, video: Path) -> None:
    run_path, manifest = run_service.create(video)
    run_service.fail(run_path, manifest, RunStage.INSPECT, ValueError("no audio stream"))

    stored = run_service.workspace.read_manifest(run_path)
    assert stored.status == RunStatus.FAILED_INSPECT
    assert stored.failure is not None
    assert stored.failure.stage == RunStage.INSPECT
    assert stored.failure.error_type == "ValueError"
    assert "no audio stream" in stored.failure.message
    assert run_path.is_dir()


def test_recovering_from_a_failure_clears_it(run_service: RunService, video: Path) -> None:
    """ADR-018: failure describes why a run is stopped now, not its history."""
    run_path, manifest = run_service.create(video)
    run_service.fail(run_path, manifest, RunStage.INSPECT, ValueError("transient"))
    run_service.advance(run_path, manifest, RunStatus.INSPECTED)

    stored = run_service.workspace.read_manifest(run_path)
    assert stored.status == RunStatus.INSPECTED
    assert stored.failure is None


def test_a_later_stage_advancing_also_clears_an_earlier_failure(
    run_service: RunService, video: Path
) -> None:
    """A manifest at AUDIO_READY must not still carry a FAILED_INSPECT record."""
    run_path, manifest = run_service.create(video)
    run_service.fail(run_path, manifest, RunStage.INSPECT, ValueError("transient"))
    run_service.advance(run_path, manifest, RunStatus.INSPECTED)
    run_service.advance(run_path, manifest, RunStatus.AUDIO_READY)

    stored = run_service.workspace.read_manifest(run_path)
    assert stored.status == RunStatus.AUDIO_READY
    assert stored.failure is None


def test_a_retry_that_fails_again_replaces_the_previous_failure(
    run_service: RunService, video: Path
) -> None:
    run_path, manifest = run_service.create(video)
    run_service.fail(run_path, manifest, RunStage.INSPECT, ValueError("first"))
    run_service.fail(run_path, manifest, RunStage.INSPECT, TypeError("second"))

    stored = run_service.workspace.read_manifest(run_path)
    assert stored.failure is not None
    assert stored.failure.error_type == "TypeError"
    assert stored.failure.message == "second"


def test_stage_records_carry_a_fingerprint(run_service: RunService, video: Path) -> None:
    run_path, manifest = run_service.create(video)
    run_service.record_stage(run_path, manifest, RunStage.TRANSCRIPTION, "abc123", "def456", 1)

    stored = run_service.workspace.read_manifest(run_path)
    record = stored.stages[RunStage.TRANSCRIPTION.value]
    assert record.fingerprint == "abc123"
    assert record.stage_config_sha256 == "def456"
    assert record.schema_version == 1


def test_unknown_manifest_schema_is_refused(run_service: RunService, video: Path) -> None:
    run_path, _ = run_service.create(video)
    manifest_path = run_path.joinpath("manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = MANIFEST_SCHEMA_VERSION + 99
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersionError, match="not interpreted"):
        run_service.workspace.read_manifest(run_path)


def test_corrupt_manifest_reports_a_clean_error(run_service: RunService, video: Path) -> None:
    run_path, _ = run_service.create(video)
    run_path.joinpath("manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptArtifactError, match="not valid JSON"):
        run_service.workspace.read_manifest(run_path)


def test_manifest_missing_required_fields_reports_a_clean_error(
    run_service: RunService, video: Path
) -> None:
    run_path, _ = run_service.create(video)
    run_path.joinpath("manifest.json").write_text(
        json.dumps({"schema_version": MANIFEST_SCHEMA_VERSION, "run_id": "x"}),
        encoding="utf-8",
    )

    with pytest.raises(CorruptArtifactError, match="not a valid manifest"):
        run_service.workspace.read_manifest(run_path)
