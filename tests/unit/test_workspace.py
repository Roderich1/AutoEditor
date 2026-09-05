from pathlib import Path

import pytest

from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.domain.exceptions import InvalidMediaError


def test_workspace_creates_expected_tree(tmp_path: Path) -> None:
    workspace = RunWorkspace(tmp_path)
    run_path = workspace.create("valid-run")

    assert run_path == tmp_path.joinpath("runs", "valid-run")
    assert run_path.joinpath("analysis", "raw").is_dir()
    assert run_path.joinpath("transcript").is_dir()


@pytest.mark.parametrize("run_id", ["", "..", ".", str(Path("nested", "run"))])
def test_workspace_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(InvalidMediaError):
        RunWorkspace(tmp_path).run_path(run_id)
