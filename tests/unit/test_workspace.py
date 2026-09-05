from __future__ import annotations

from pathlib import Path

import pytest

from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.domain.exceptions import (
    CorruptArtifactError,
    InvalidRunIdError,
    RunNotFoundError,
)


def test_workspace_creates_expected_tree(tmp_path: Path) -> None:
    workspace = RunWorkspace(tmp_path)
    run_path = workspace.create("valid-run")

    assert run_path == tmp_path.joinpath("runs", "valid-run")
    assert run_path.joinpath("analysis", "raw").is_dir()
    assert run_path.joinpath("transcript").is_dir()
    assert run_path.joinpath("logs").is_dir()


@pytest.mark.parametrize("run_id", ["", "..", ".", str(Path("nested", "run")), "a/b"])
def test_workspace_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(InvalidRunIdError):
        RunWorkspace(tmp_path).run_path(run_id)


def test_requiring_a_missing_run_is_not_a_media_error(tmp_path: Path) -> None:
    """A missing run is a missing run, not invalid media."""
    with pytest.raises(RunNotFoundError, match="Run does not exist"):
        RunWorkspace(tmp_path).require("20260101T000000-absent-abc123")


def test_reading_a_manifest_that_does_not_exist(tmp_path: Path) -> None:
    workspace = RunWorkspace(tmp_path)
    run_path = workspace.create("valid-run")

    with pytest.raises(CorruptArtifactError, match="has no manifest"):
        workspace.read_manifest(run_path)
